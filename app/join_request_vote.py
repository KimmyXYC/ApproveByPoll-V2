import asyncio
import html

from loguru import logger
from telebot import types

from app_conf import settings
from setting.telegrambot import BotSetting
from utils.i18n import t
from utils.postgres import BotDatabase


class JoinRequestVote:
    def __init__(
        self, bot, request: types.ChatJoinRequest, uuid: str, group_settings: dict
    ):
        self.bot = bot
        self.request = request
        self.uuid = uuid
        self.group_settings = group_settings
        self.language = group_settings.get("language")
        self.vote_time = int(group_settings.get("vote_time", 600))
        self.advanced_vote_enabled = bool(group_settings.get("advanced_vote", False))
        self.message1 = None
        self.message2 = None
        self.message3 = None
        self.message4 = None
        self._manual_resolved = asyncio.Event()
        self._vote_lock = asyncio.Lock()
        self._yes_voters: dict[int, str] = {}
        self._no_voters: dict[int, str] = {}
        self.log_message_id: int | None = None

    @property
    def chat_id(self) -> int:
        return self.request.chat.id

    @property
    def user_id(self) -> int:
        return self.request.from_user.id

    def _user_display(self, user: types.User) -> str:
        if user.username:
            return f"@{user.username}"
        full_name = html.escape(user.full_name)
        return f'<a href="tg://user?id={user.id}">{full_name}</a>'

    def _user_full_name_link(self, user_id: int, full_name: str) -> str:
        return f'<a href="tg://user?id={user_id}">{html.escape(full_name)}</a>'

    def _admin_display(self, user: types.User) -> str:
        if user.username:
            return f"@{user.username}"
        return html.escape(user.full_name)

    def _vote_minutes(self) -> int:
        minutes = self.vote_time // 60
        return max(minutes, 1)

    def _build_log_text(
        self,
        status: str,
        yes_votes: int | None = None,
        no_votes: int | None = None,
        admin_id: int | None = None,
        admin_name: str | None = None,
    ) -> str:
        applicant = self.request.from_user
        lines = [
            f"<b>Chat:</b> {html.escape(self.request.chat.title or str(self.request.chat.id))}",
            f"<b>User:</b> {self._user_full_name_link(applicant.id, applicant.full_name)}",
            f"<b>User ID:</b> <code>{applicant.id}</code>",
            f"<b>Status:</b> {status}",
        ]
        if yes_votes is not None and no_votes is not None:
            lines.append(f"<b>Result:</b> Allow : Deny = {yes_votes} : {no_votes}")
        if admin_id is not None and admin_name:
            lines.append(
                f"<b>Admin:</b> {self._user_full_name_link(admin_id, admin_name)}"
            )
        return "\n".join(lines)

    def _log_channel_config(self) -> tuple[bool, int | None, int | None]:
        enabled = bool(settings.get("logchannel.enable", False))
        channel_id = settings.get("logchannel.channel_id", None)
        thread_id = settings.get("logchannel.message_thread_id", None)

        channel_id_value = int(channel_id) if channel_id is not None else None
        thread_id_value = int(thread_id) if thread_id is not None else None
        return enabled, channel_id_value, thread_id_value

    async def _send_pending_log(self):
        enabled, channel_id, thread_id = self._log_channel_config()
        if not enabled or channel_id is None:
            return

        kwargs = {
            "chat_id": channel_id,
            "text": self._build_log_text(status="Pending"),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id is not None and thread_id != 0:
            kwargs["message_thread_id"] = thread_id

        try:
            message = await self.bot.send_message(**kwargs)
            self.log_message_id = message.message_id
        except Exception:
            self.log_message_id = None

    async def _edit_log_result(
        self,
        status: str,
        yes_votes: int | None = None,
        no_votes: int | None = None,
        admin_id: int | None = None,
        admin_name: str | None = None,
    ):
        enabled, channel_id, _ = self._log_channel_config()
        if not enabled or channel_id is None or self.log_message_id is None:
            return

        try:
            await self.bot.edit_message_text(
                chat_id=channel_id,
                message_id=self.log_message_id,
                text=self._build_log_text(
                    status=status,
                    yes_votes=yes_votes,
                    no_votes=no_votes,
                    admin_id=admin_id,
                    admin_name=admin_name,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            return

    async def _safe_delete_message(self, chat_id: int, message_id: int | None):
        if not message_id:
            return
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            return

    async def _safe_stop_poll(self):
        if self.advanced_vote_enabled:
            return None
        if not self.message2:
            return None
        try:
            return await self.bot.stop_poll(
                chat_id=self.chat_id,
                message_id=self.message2.message_id,
            )
        except Exception:
            return None

    async def _close_failed_request(self):
        applicant = self.request.from_user
        applicant_display = self._user_display(applicant)

        if self.message1:
            try:
                await self._refresh_message1(
                    "jr_status_rejected",
                    user=applicant_display,
                    user_id=applicant.id,
                )
            except Exception:
                pass

        try:
            await BotDatabase.update_join_request(
                uuid=self.uuid,
                result=False,
                yes_votes=0,
                no_votes=0,
            )
        except Exception:
            pass

        try:
            await self._apply_join_result(False)
        except Exception:
            pass

    async def _apply_join_result(self, approved: bool):
        if approved:
            await self.bot.approve_chat_join_request(
                chat_id=self.chat_id,
                user_id=self.user_id,
            )
        else:
            await self.bot.decline_chat_join_request(
                chat_id=self.chat_id,
                user_id=self.user_id,
            )

    async def _check_invite_permission(self, user_id: int) -> bool:
        member = await self.bot.get_chat_member(chat_id=self.chat_id, user_id=user_id)
        if member.status == "creator":
            return True
        if member.status != "administrator":
            return False
        return bool(getattr(member, "can_invite_users", False))

    async def _is_group_member(self, user_id: int) -> bool:
        member = await self.bot.get_chat_member(chat_id=self.chat_id, user_id=user_id)
        return member.status not in {"left", "kicked"}

    async def _get_bot_username(self) -> str:
        if BotSetting.bot_username:
            return BotSetting.bot_username
        me = await self.bot.get_me()
        return me.username

    def _has_voted(self, user_id: int) -> bool:
        return user_id in self._yes_voters or user_id in self._no_voters

    async def _build_advanced_vote_keyboard(self) -> types.InlineKeyboardMarkup:
        bot_username = await self._get_bot_username()
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton(
                text=t(self.language, "jr_poll_yes"),
                callback_data=f"jrv {self.uuid} yes",
            ),
            types.InlineKeyboardButton(
                text=t(self.language, "jr_poll_no"),
                callback_data=f"jrv {self.uuid} no",
            ),
        )
        keyboard.add(
            types.InlineKeyboardButton(
                text=t(self.language, "jr_live_result"),
                url=f"https://t.me/{bot_username}?start=jrres_{self.uuid}",
            )
        )
        return keyboard

    async def _refresh_message1(self, key: str, **kwargs):
        if not self.message1:
            return
        await self.bot.edit_message_text(
            chat_id=self.chat_id,
            message_id=self.message1.message_id,
            text=t(self.language, key, **kwargs),
            parse_mode="HTML",
            reply_markup=None,
        )

    async def _notify_applicant(self, text_key: str):
        if not self.message3:
            return
        try:
            await self.bot.send_message(
                chat_id=self.user_id,
                text=t(self.language, text_key),
                reply_to_message_id=self.message3.message_id,
            )
        except Exception:
            return

    async def run(self):
        applicant = self.request.from_user
        applicant_display = self._user_display(applicant)
        logger.debug(
            "join request flow start uuid={} chat_id={} user_id={} advanced_vote={} anonymous_vote={}",
            self.uuid,
            self.chat_id,
            self.user_id,
            self.advanced_vote_enabled,
            bool(self.group_settings.get("anonymous_vote", True)),
        )

        msg1_text = t(
            self.language,
            "jr_requesting",
            user=applicant_display,
            user_id=applicant.id,
        )
        keyboard = types.InlineKeyboardMarkup(row_width=3)
        keyboard.add(
            types.InlineKeyboardButton(
                "Approve", callback_data=f"jr {self.uuid} approve"
            ),
            types.InlineKeyboardButton(
                "Reject", callback_data=f"jr {self.uuid} reject"
            ),
            types.InlineKeyboardButton("Ban", callback_data=f"jr {self.uuid} ban"),
        )

        self.message1 = await self.bot.send_message(
            chat_id=self.chat_id,
            text=msg1_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.debug(
            "message1 sent uuid={} chat_id={} message_id={}",
            self.uuid,
            self.chat_id,
            self.message1.message_id,
        )
        await self._send_pending_log()

        if self.advanced_vote_enabled:
            try:
                self.message2 = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=t(self.language, "jr_poll_question"),
                    reply_to_message_id=self.message1.message_id,
                    reply_markup=await self._build_advanced_vote_keyboard(),
                    protect_content=True,
                )
                logger.debug(
                    "advanced message2 sent uuid={} chat_id={} message_id={}",
                    self.uuid,
                    self.chat_id,
                    self.message2.message_id,
                )
            except Exception:
                logger.exception(
                    "failed to send advanced message2 uuid={} chat_id={}",
                    self.uuid,
                    self.chat_id,
                )
                await self._close_failed_request()
                return
        else:
            try:
                self.message2 = await self.bot.send_poll(
                    chat_id=self.chat_id,
                    question=t(self.language, "jr_poll_question"),
                    options=[
                        t(self.language, "jr_poll_yes"),
                        t(self.language, "jr_poll_no"),
                    ],
                    is_anonymous=bool(self.group_settings.get("anonymous_vote", True)),
                    protect_content=True,
                    allows_multiple_answers=False,
                    reply_to_message_id=self.message1.message_id,
                )
                logger.debug(
                    "poll message2 sent uuid={} chat_id={} message_id={}",
                    self.uuid,
                    self.chat_id,
                    self.message2.message_id,
                )
            except Exception:
                logger.exception(
                    "failed to send poll message2, fallback to advanced uuid={} chat_id={}",
                    self.uuid,
                    self.chat_id,
                )
                self.advanced_vote_enabled = True
                try:
                    self.message2 = await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=t(self.language, "jr_poll_question"),
                        reply_to_message_id=self.message1.message_id,
                        reply_markup=await self._build_advanced_vote_keyboard(),
                        protect_content=True,
                    )
                    logger.warning(
                        "poll fallback activated, advanced message2 sent uuid={} chat_id={} message_id={}",
                        self.uuid,
                        self.chat_id,
                        self.message2.message_id,
                    )
                except Exception:
                    logger.exception(
                        "failed to send fallback advanced message2 uuid={} chat_id={}",
                        self.uuid,
                        self.chat_id,
                    )
                    await self._close_failed_request()
                    return

        if self.group_settings.get("pin_msg", False):
            try:
                await self.bot.pin_chat_message(
                    chat_id=self.chat_id,
                    message_id=self.message2.message_id,
                    disable_notification=True,
                )
                logger.debug(
                    "message2 pinned uuid={} chat_id={} message_id={}",
                    self.uuid,
                    self.chat_id,
                    self.message2.message_id,
                )
            except Exception:
                logger.exception(
                    "failed to pin message2 uuid={} chat_id={} message_id={}",
                    self.uuid,
                    self.chat_id,
                    self.message2.message_id if self.message2 else None,
                )
                pass

        try:
            self.message3 = await self.bot.send_message(
                chat_id=self.user_id,
                text=t(
                    self.language,
                    "jr_apply_notice",
                    group_name=self.request.chat.title,
                    vote_minutes=self._vote_minutes(),
                ),
            )
            logger.debug(
                "message3 sent to applicant uuid={} user_id={} message_id={}",
                self.uuid,
                self.user_id,
                self.message3.message_id,
            )
        except Exception:
            logger.exception(
                "failed to send message3 to applicant uuid={} user_id={}",
                self.uuid,
                self.user_id,
            )
            self.message3 = None

        try:
            await asyncio.wait_for(self._manual_resolved.wait(), timeout=self.vote_time)
            return
        except asyncio.TimeoutError:
            pass

        waiting = await BotDatabase.get_join_request_waiting_by_uuid(self.uuid)
        if waiting is not True:
            logger.debug(
                "join request already resolved before timeout uuid={}", self.uuid
            )
            return

        yes_votes = 0
        no_votes = 0
        if self.advanced_vote_enabled:
            async with self._vote_lock:
                yes_votes = len(self._yes_voters)
                no_votes = len(self._no_voters)
            if self.message2:
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message2.message_id,
                        text=t(
                            self.language,
                            "jr_final_votes",
                            yes_votes=yes_votes,
                            no_votes=no_votes,
                        ),
                    )
                except Exception:
                    pass
        else:
            poll_result = await self._safe_stop_poll()

            if poll_result and poll_result.options and len(poll_result.options) >= 2:
                yes_votes = int(poll_result.options[0].voter_count)
                no_votes = int(poll_result.options[1].voter_count)
            elif (
                self.message2
                and self.message2.poll
                and len(self.message2.poll.options) >= 2
            ):
                yes_votes = int(self.message2.poll.options[0].voter_count)
                no_votes = int(self.message2.poll.options[1].voter_count)

        total_votes = yes_votes + no_votes
        min_voters = int(self.group_settings.get("mini_voters", 1))
        logger.debug(
            "vote result collected uuid={} yes_votes={} no_votes={} total={} min_voters={}",
            self.uuid,
            yes_votes,
            no_votes,
            total_votes,
            min_voters,
        )

        if total_votes < min_voters:
            self.message4 = await self.bot.send_message(
                chat_id=self.chat_id,
                text=t(self.language, "jr_not_enough_voters"),
                reply_to_message_id=self.message1.message_id,
            )
            await self._refresh_message1(
                "jr_status_not_enough_voters",
                user=applicant_display,
                user_id=applicant.id,
            )
            await self._notify_applicant("jr_no_votes_private")
            await BotDatabase.update_join_request(
                uuid=self.uuid,
                result=False,
                yes_votes=yes_votes,
                no_votes=no_votes,
            )
            await self._apply_join_result(False)
            await self._edit_log_result(
                status="Denied",
                yes_votes=yes_votes,
                no_votes=no_votes,
            )
        else:
            if yes_votes > no_votes:
                status_key = "jr_status_approved"
                group_key = "jr_group_approved"
                private_key = "jr_private_approved"
                approved = True
            elif yes_votes == no_votes:
                status_key = "jr_status_tie"
                group_key = "jr_group_tie"
                private_key = "jr_private_rejected"
                approved = False
            else:
                status_key = "jr_status_rejected"
                group_key = "jr_group_rejected"
                private_key = "jr_private_rejected"
                approved = False

            self.message4 = await self.bot.send_message(
                chat_id=self.chat_id,
                text=t(self.language, group_key),
                reply_to_message_id=self.message1.message_id,
            )
            await self._refresh_message1(
                status_key,
                user=applicant_display,
                user_id=applicant.id,
            )
            await self._notify_applicant(private_key)
            await BotDatabase.update_join_request(
                uuid=self.uuid,
                result=approved,
                yes_votes=yes_votes,
                no_votes=no_votes,
            )
            await self._apply_join_result(approved)
            await self._edit_log_result(
                status="Approved" if approved else "Denied",
                yes_votes=yes_votes,
                no_votes=no_votes,
            )

        await asyncio.sleep(60)
        if self.message2:
            await self._safe_delete_message(self.chat_id, self.message2.message_id)
        if self.message4:
            await self._safe_delete_message(self.chat_id, self.message4.message_id)
        logger.debug("join request flow completed uuid={}", self.uuid)

    async def handle_action(self, call: types.CallbackQuery, action: str):
        if not await self._check_invite_permission(call.from_user.id):
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text=t(self.language, "insufficient_permissions"),
                show_alert=True,
            )
            return

        waiting = await BotDatabase.get_join_request_waiting_by_uuid(self.uuid)
        if not waiting:
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text="Expired",
                show_alert=False,
            )
            return

        admin_display = self._admin_display(call.from_user)
        applicant = self.request.from_user
        applicant_display = self._user_display(applicant)

        if action == "approve":
            await BotDatabase.update_join_request(
                uuid=self.uuid,
                result=True,
                admin=call.from_user.id,
            )
            await self._refresh_message1(
                "jr_status_admin_approved",
                user=applicant_display,
                user_id=applicant.id,
                admin=admin_display,
            )
            await self._notify_applicant("jr_private_approved")
            await self._apply_join_result(True)
            await self._edit_log_result(
                status="Approved",
                admin_id=call.from_user.id,
                admin_name=call.from_user.full_name,
            )
        elif action == "reject":
            await BotDatabase.update_join_request(
                uuid=self.uuid,
                result=False,
                admin=call.from_user.id,
            )
            await self._refresh_message1(
                "jr_status_admin_rejected",
                user=applicant_display,
                user_id=applicant.id,
                admin=admin_display,
            )
            await self._notify_applicant("jr_private_rejected")
            await self._apply_join_result(False)
            await self._edit_log_result(
                status="Denied",
                admin_id=call.from_user.id,
                admin_name=call.from_user.full_name,
            )
        elif action == "ban":
            await BotDatabase.update_join_request(
                uuid=self.uuid,
                result=False,
                admin=call.from_user.id,
            )
            await self._refresh_message1(
                "jr_status_admin_banned",
                user=applicant_display,
                user_id=applicant.id,
                admin=admin_display,
            )
            await self._notify_applicant("jr_private_rejected")
            await self._apply_join_result(False)
            await self.bot.ban_chat_member(chat_id=self.chat_id, user_id=self.user_id)
            await self._edit_log_result(
                status="Denied",
                admin_id=call.from_user.id,
                admin_name=call.from_user.full_name,
            )
        else:
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text="Unsupported action",
            )
            return

        self._manual_resolved.set()
        await self._safe_stop_poll()
        if self.message2:
            await self._safe_delete_message(self.chat_id, self.message2.message_id)

        await self.bot.answer_callback_query(callback_query_id=call.id, text="Done")

    async def handle_vote(self, call: types.CallbackQuery, option: str):
        if not self.advanced_vote_enabled:
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text="Expired",
            )
            return

        waiting = await BotDatabase.get_join_request_waiting_by_uuid(self.uuid)
        if waiting is not True:
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text="Expired",
            )
            return

        if option not in {"yes", "no"}:
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text="Invalid vote",
            )
            return

        if not await self._is_group_member(call.from_user.id):
            await self.bot.answer_callback_query(
                callback_query_id=call.id,
                text=t(self.language, "insufficient_permissions"),
                show_alert=True,
            )
            return

        async with self._vote_lock:
            if self._has_voted(call.from_user.id):
                await self.bot.answer_callback_query(
                    callback_query_id=call.id,
                    text=t(self.language, "jr_already_voted"),
                    show_alert=True,
                )
                return

            full_name = call.from_user.full_name
            if option == "yes":
                self._yes_voters[call.from_user.id] = full_name
            else:
                self._no_voters[call.from_user.id] = full_name

        await self.bot.answer_callback_query(
            callback_query_id=call.id,
            text=t(self.language, "jr_vote_recorded"),
        )

    async def handle_realtime_result_request(self, message: types.Message):
        waiting = await BotDatabase.get_join_request_waiting_by_uuid(self.uuid)
        if waiting is not True:
            await self.bot.reply_to(message, "Expired")
            return

        user_id = message.from_user.id if message.from_user else None
        if user_id is None:
            return

        if not await self._is_group_member(user_id):
            await self.bot.reply_to(
                message, t(self.language, "insufficient_permissions")
            )
            return

        async with self._vote_lock:
            if not self._has_voted(user_id):
                await self.bot.reply_to(message, t(self.language, "jr_not_voted"))
                return

            yes_votes = len(self._yes_voters)
            no_votes = len(self._no_voters)

            if self.group_settings.get("anonymous_vote", True):
                text = t(
                    self.language,
                    "jr_live_votes_anonymous",
                    yes_votes=yes_votes,
                    no_votes=no_votes,
                )
            else:
                yes_names = "\n".join(self._yes_voters.values()) or "-"
                no_names = "\n".join(self._no_voters.values()) or "-"
                text = t(
                    self.language,
                    "jr_live_votes_public",
                    yes_votes=yes_votes,
                    no_votes=no_votes,
                    yes_names=yes_names,
                    no_names=no_names,
                )

        await self.bot.reply_to(message, text)
