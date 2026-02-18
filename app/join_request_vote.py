import asyncio
import html

from telebot import types

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
        self.message1 = None
        self.message2 = None
        self.message3 = None
        self.message4 = None
        self._manual_resolved = asyncio.Event()

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

    def _admin_display(self, user: types.User) -> str:
        if user.username:
            return f"@{user.username}"
        return html.escape(user.full_name)

    def _vote_minutes(self) -> int:
        minutes = self.vote_time // 60
        return max(minutes, 1)

    async def _safe_delete_message(self, chat_id: int, message_id: int | None):
        if not message_id:
            return
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            return

    async def _safe_stop_poll(self):
        if not self.message2:
            return None
        try:
            return await self.bot.stop_poll(
                chat_id=self.chat_id,
                message_id=self.message2.message_id,
            )
        except Exception:
            return None

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

        self.message2 = await self.bot.send_poll(
            chat_id=self.chat_id,
            question=t(self.language, "jr_poll_question"),
            options=[
                t(self.language, "jr_poll_yes"),
                t(self.language, "jr_poll_no"),
            ],
            is_anonymous=bool(self.group_settings.get("anonymous_vote", True)),
            protect_content=True,
            allow_multiple_answers=False,
            reply_to_message_id=self.message1.message_id,
        )

        if self.group_settings.get("pin_msg", False):
            try:
                await self.bot.pin_chat_message(
                    chat_id=self.chat_id,
                    message_id=self.message2.message_id,
                    disable_notification=True,
                )
            except Exception:
                pass

        self.message3 = await self.bot.send_message(
            chat_id=self.user_id,
            text=t(
                self.language,
                "jr_apply_notice",
                group_name=self.request.chat.title,
                vote_minutes=self._vote_minutes(),
            ),
        )

        try:
            await asyncio.wait_for(self._manual_resolved.wait(), timeout=self.vote_time)
            return
        except asyncio.TimeoutError:
            pass

        waiting = await BotDatabase.get_join_request_waiting_by_uuid(self.uuid)
        if waiting is not True:
            return

        poll_result = await self._safe_stop_poll()
        yes_votes = 0
        no_votes = 0

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

        await asyncio.sleep(60)
        if self.message2:
            await self._safe_delete_message(self.chat_id, self.message2.message_id)
        if self.message4:
            await self._safe_delete_message(self.chat_id, self.message4.message_id)

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
            await self._apply_join_result(True)
            await self._notify_applicant("jr_private_approved")
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
            await self._apply_join_result(False)
            await self._notify_applicant("jr_private_rejected")
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
            await self._apply_join_result(False)
            await self.bot.ban_chat_member(chat_id=self.chat_id, user_id=self.user_id)
            await self._notify_applicant("jr_private_rejected")
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
