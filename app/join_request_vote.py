"""A recoverable join-request state machine; no authoritative in-memory votes."""

import html

from telebot import types

from app.recovery_operations import PollUnavailable, Rejected, member_present
from utils.i18n import t
from utils.duration import format_duration


def keyboard(buttons):
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        *(
            types.InlineKeyboardButton(label, callback_data=data)
            for label, data in buttons
        )
    )
    return markup.to_json()


class JoinRequestVote:
    def __init__(self, manager, task):
        self.manager = manager
        self.store = manager.store
        self.bot = manager.bot
        self.ops = manager.ops
        self.task = task

    @property
    def language(self):
        return self.task.get("settings", {}).get("language", "en_US")

    def text(self, key, **kwargs):
        return t(self.language, key, **kwargs)

    def applicant(self):
        user = self.task.get("applicant", {})
        name = html.escape(user.get("name") or str(self.task["user_id"]))
        return f'<a href="tg://user?id={self.task["user_id"]}">{name}</a>'

    async def save(self):
        await self.store.save(self.task, self.manager.clock())

    async def send(self, name, chat_id, text, *, core=False, **kwargs):
        ref = await self.ops.call(
            self.task,
            name,
            "send_message",
            dict(chat_id=chat_id, text=text, **kwargs),
            policy="core_send" if core else "aux_send",
            optional=not core,
        )
        if ref:
            self.task["refs"][name] = ref
            await self.save()
        return ref

    async def edit(self, name, ref_name, text, **kwargs):
        ref = self.task["refs"].get(ref_name)
        if ref:
            return await self.ops.call(
                self.task,
                f"{name}_{ref['chat_id']}_{ref['message_id']}",
                "edit_message_text",
                dict(
                    chat_id=ref["chat_id"],
                    message_id=ref["message_id"],
                    text=text,
                    **kwargs,
                ),
                optional=True,
            )

    def action_keyboard(self, recovery=False):
        uid = self.task["uuid"]
        buttons = [
            (self.text("recover_approve"), f"jr {uid} approve"),
            (self.text("recover_reject"), f"jr {uid} reject"),
            (self.text("recover_ban"), f"jr {uid} ban"),
        ]
        if recovery and self.task.get("applied"):
            buttons = []
        markup = types.InlineKeyboardMarkup.de_json(keyboard(buttons))
        if recovery:
            markup.row(
                types.InlineKeyboardButton(
                    self.text("recover_retry"), callback_data=f"jr {uid} retry"
                )
            )
        return markup.to_json()

    def recovery_reason(self):
        reason = self.task.get("error", "")
        if self.task.get("legacy") or reason.startswith(
            ("legacy", "corrupt", "invalid_task")
        ):
            return self.text("recover_reason_snapshot")
        if reason.startswith(("uncertain_native_poll", "uncertain_advanced_poll")):
            return self.text("recover_reason_delivery")
        if "poll" in reason:
            return self.text("recover_reason_poll")
        if reason.startswith("uncertain_apply"):
            return self.text("recover_reason_approval")
        return self.text("recover_reason_api")

    async def prepare(self):
        task = self.task
        if task.get("legacy"):
            await self.ops.attention(task, "legacy_snapshot_missing")
        await self.send(
            "intro",
            task["group_id"],
            self.text("jr_requesting", user=self.applicant(), user_id=task["user_id"]),
            parse_mode="HTML",
            reply_markup=self.action_keyboard(),
        )
        log = task.get("log")
        if log:
            params = (
                {"message_thread_id": log["thread_id"]} if log.get("thread_id") else {}
            )
            await self.send(
                "log",
                log["chat_id"],
                self.log_text("Pending"),
                parse_mode="HTML",
                **params,
            )
        if "deadline" not in task:
            task["deadline"] = int(self.manager.clock()) + task["settings"].get(
                "vote_time", 600
            )
            await self.save()
        ref = task["refs"].get("vote")
        if not ref:
            if task["deadline"] <= self.manager.clock():
                name = "native_poll" if task["mode"] == "poll" else "advanced_poll"
                operation = await self.store.operation(task["uuid"], name)
                if not operation or operation["state"] == "pending":
                    await self.ops.attention(task, "preparation_deadline_elapsed")
            reply = task["refs"].get("intro")
            params = dict(chat_id=task["group_id"], protect_content=True)
            if reply:
                params["reply_to_message_id"] = reply["message_id"]
            if task["mode"] == "poll":
                try:
                    ref = await self.ops.call(
                        task,
                        "native_poll",
                        "send_poll",
                        dict(
                            **params,
                            question=self.text("jr_poll_question"),
                            options=[self.text("jr_poll_yes"), self.text("jr_poll_no")],
                            is_anonymous=task["settings"].get("anonymous_vote", True),
                            allows_multiple_answers=False,
                            close_date=int(task["deadline"]),
                        ),
                        policy="core_send",
                    )
                    task["poll_id"] = ref["poll_id"]
                    task["poll"] = ref["poll"]
                except Rejected:
                    task["mode"] = "advanced"
                    await self.save()
            if task["mode"] == "advanced":
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    types.InlineKeyboardButton(
                        self.text("jr_poll_yes"),
                        callback_data=f"jrv {task['uuid']} yes",
                    ),
                    types.InlineKeyboardButton(
                        self.text("jr_poll_no"), callback_data=f"jrv {task['uuid']} no"
                    ),
                )
                if self.manager.username:
                    markup.add(
                        types.InlineKeyboardButton(
                            self.text("jr_live_result"),
                            url=f"https://t.me/{self.manager.username}?start=jrres_{task['uuid']}",
                        )
                    )
                ref = await self.ops.call(
                    task,
                    "advanced_poll",
                    "send_message",
                    dict(
                        **params,
                        text=self.text("jr_poll_question"),
                        reply_markup=markup.to_json(),
                    ),
                    policy="core_send",
                )
            task["refs"]["vote"] = ref
            await self.save()
        task["phase"] = "voting"
        await self.save()

    async def voting_setup(self):
        task = self.task
        if task["settings"].get("pin_msg") and task["refs"].get("vote"):
            ref = task["refs"]["vote"]
            await self.ops.call(
                task,
                "pin",
                "pin_chat_message",
                dict(
                    chat_id=ref["chat_id"],
                    message_id=ref["message_id"],
                    disable_notification=True,
                ),
                optional=True,
            )
        # A failed private message or pin must not restart the deadline.
        await self.send(
            "applicant",
            task.get("user_chat_id", task["user_id"]),
            self.text(
                "jr_apply_notice",
                group_name=task.get("chat_title") or str(task["group_id"]),
                duration=format_duration(
                    self.language, task["settings"].get("vote_time", 600)
                ),
            ),
            reply_markup=keyboard(
                [(self.text("jr_check_status"), f"jrs {task['uuid']}")]
            ),
        )

    async def settle(self):
        task = self.task
        if task.get("poll_probe_next", 0) > self.manager.clock():
            return
        # Recover external approvals even if their member update was missed.
        member = await self.ops.call(
            task,
            "member_before_settle",
            "get_chat_member",
            dict(chat_id=task["group_id"], user_id=task["user_id"]),
        )
        if member and (
            member["status"] in {"member", "administrator", "creator"}
            or (member["status"] == "restricted" and member["is_member"])
        ):
            await self.store.finish_external(
                task["uuid"], "approve", "member_present", self.manager.clock()
            )
            return
        # A negative observation is not durable proof that the request is still
        # pending. Refresh it on the next settlement attempt, including reboot.
        operation = await self.store.operation(task["uuid"], "member_before_settle")
        operation.update(state="pending", attempts=0, next_at=0)
        await self.store.save_operation(task["uuid"], "member_before_settle", operation)
        if task["mode"] == "poll" and not task.get("poll", {}).get("closed"):
            if task.get("poll_probe_next", 0) > self.manager.clock():
                return
            ref = task["refs"].get("vote")
            if not ref:
                await self.ops.attention(task, "poll_reference_missing")
            try:
                result = await self.ops.call(
                    task,
                    "stop_poll",
                    "stop_poll",
                    dict(chat_id=ref["chat_id"], message_id=ref["message_id"]),
                )
            except PollUnavailable:
                # Auto-closed polls may not generate a final Update. Telegram
                # returns the actual Message (including Poll) when its keyboard
                # changes. Never infer counts from "can't be stopped" itself.
                probe = task.get("poll_probe", 0)
                message = await self.ops.call(
                    task,
                    f"read_poll_controls_{probe}",
                    "edit_message_reply_markup",
                    dict(
                        chat_id=ref["chat_id"],
                        message_id=ref["message_id"],
                        reply_markup=self.action_keyboard(),
                    ),
                )
                # These controls only trigger a fresh Message response. Native
                # polls have no query button; remove the temporary markup even
                # when the response contains no usable final totals.
                await self.clear_poll_markup()
                result = message.get("poll") if isinstance(message, dict) else None
                if not result or not result.get("closed"):
                    # A retry after an interrupted edit can return "not modified".
                    # The cleared keyboard allows a fresh edit next time.
                    task["poll_probe"] = probe + 1
                    attempts = task.get("poll_probe_attempts", 0) + 1
                    task["poll_probe_attempts"] = attempts
                    task["poll_probe_next"] = self.manager.clock() + min(
                        60, 2 ** min(attempts, 6)
                    )
                    await self.save()
                    if attempts < 10:
                        return
            if not result or not result.get("closed"):
                await self.ops.attention(task, "final_poll_missing")
            await self.store.poll(
                task["poll_id"], result, 2**63 - 1, self.manager.clock()
            )
        if task["mode"] == "poll":
            # Also finish clearing if a crash happened after the final Poll was
            # persisted but before its temporary keyboard was removed.
            await self.clear_poll_markup()
        await self.store.decide(task["uuid"], None, self.manager.clock())

    async def clear_poll_markup(self):
        ref = self.task["refs"].get("vote")
        probe = self.task.get("poll_probe", 0)
        read_name = f"read_poll_controls_{probe}"
        operation = await self.store.operation(self.task["uuid"], read_name)
        if operation is None:
            # Clear query buttons persisted by the earlier recovery implementation.
            read_name = f"read_poll_{probe}"
            operation = await self.store.operation(self.task["uuid"], read_name)
        if ref and operation and operation["state"] == "success":
            await self.ops.call(
                self.task,
                f"clear_{read_name}",
                "edit_message_reply_markup",
                dict(
                    chat_id=ref["chat_id"],
                    message_id=ref["message_id"],
                    reply_markup=types.InlineKeyboardMarkup().to_json(),
                ),
                optional=True,
            )

    async def resolve(self):
        task = self.task
        d = task["decision"]
        action = d["action"]
        # banChatMember itself rejects/prevents membership. Do not first issue an
        # ambiguous decline that could prevent completion of a confirmed ban.
        method = {
            "approve": "approve_chat_join_request",
            "reject": "decline_chat_join_request",
            "ban": "ban_chat_member",
        }[action]
        await self.ops.call(
            task,
            "apply_" + str(d.get("generation", 0)),
            method,
            dict(chat_id=task["group_id"], user_id=task["user_id"]),
            policy="approval",
        )
        await self.store.finish_approval(task["uuid"], self.manager.clock())

    def result_keys(self):
        d = self.task["decision"]
        if d.get("evidence") == "user_deactivated":
            return (
                "jr_status_user_deactivated",
                "jr_user_deactivated_notice",
                "jr_user_deactivated_notice",
            )
        if d.get("reason") == "external":
            outcome = "approved" if d["action"] == "approve" else "closed"
            return (
                f"jr_status_external_{outcome}",
                f"jr_external_{outcome}_notice",
                f"jr_external_{outcome}_notice",
            )
        if d.get("admin_id"):
            status = {
                "approve": "jr_status_admin_approved",
                "reject": "jr_status_admin_rejected",
                "ban": "jr_status_admin_banned",
            }[d["action"]]
            return (
                status,
                "jr_group_approved"
                if d["action"] == "approve"
                else "jr_group_rejected",
                "jr_private_approved"
                if d["action"] == "approve"
                else "jr_private_rejected",
            )
        if d["reason"] == "insufficient":
            return (
                "jr_status_not_enough_voters",
                "jr_not_enough_voters",
                "jr_no_votes_private",
            )
        if d["reason"] == "tie":
            return "jr_status_tie", "jr_group_tie", "jr_private_rejected"
        return (
            ("jr_status_approved", "jr_group_approved", "jr_private_approved")
            if d["action"] == "approve"
            else ("jr_status_rejected", "jr_group_rejected", "jr_private_rejected")
        )

    def log_text(self, status):
        task = self.task
        lines = [
            f"<b>Chat:</b> {html.escape(task.get('chat_title') or str(task['group_id']))}",
            f"<b>User:</b> {self.applicant()}",
            f"<b>User ID:</b> <code>{task['user_id']}</code>",
            f"<b>Status:</b> {status}",
        ]
        d = task.get("decision", {})
        if d.get("yes") is not None:
            lines.append(f"<b>Result:</b> Allow : Deny = {d['yes']} : {d['no']}")
        if d.get("admin_id"):
            lines.append(
                f"<b>Admin:</b> {html.escape(d.get('admin_name', str(d['admin_id'])))}"
            )
        return "\n".join(lines)

    async def cleanup(self):
        task = self.task
        status, group_key, private_key = self.result_keys()
        d = task["decision"]
        await self.edit(
            "result_intro",
            "intro",
            self.text(
                status,
                user=self.applicant(),
                user_id=task["user_id"],
                admin=html.escape(d.get("admin_name", "")),
            ),
            parse_mode="HTML",
            reply_markup=types.InlineKeyboardMarkup().to_json(),
        )
        ref = task["refs"].get("vote")
        if ref:
            if task["mode"] == "poll":
                await self.clear_poll_markup()
                # Admin decisions do not need poll totals; closing is cleanup only.
                await self.ops.call(
                    task,
                    "close_after_decision",
                    "stop_poll",
                    dict(chat_id=ref["chat_id"], message_id=ref["message_id"]),
                    optional=True,
                )
            else:
                await self.edit(
                    "close_buttons",
                    "vote",
                    self.text(group_key)
                    if d.get("reason") == "external"
                    else self.text(
                        "jr_final_votes",
                        yes_votes=d.get("yes", 0),
                        no_votes=d.get("no", 0),
                    ),
                    reply_markup=types.InlineKeyboardMarkup().to_json(),
                )
            if task.get("settings", {}).get("pin_msg"):
                await self.ops.call(
                    task,
                    "unpin",
                    "unpin_chat_message",
                    dict(chat_id=ref["chat_id"], message_id=ref["message_id"]),
                    optional=True,
                )
        await self.send("result", task["group_id"], self.text(group_key))
        applicant = task["refs"].get("applicant")
        if applicant and d.get("evidence") != "user_deactivated":
            await self.send(
                "private_result", applicant["chat_id"], self.text(private_key)
            )
        await self.edit(
            "log_result",
            "log",
            self.log_text(
                "Closed: applicant account deactivated"
                if d.get("evidence") == "user_deactivated"
                else (
                    "Approved externally"
                    if d["action"] == "approve"
                    else "Closed externally; outcome unknown"
                )
                if d.get("reason") == "external"
                else ("Approved" if d["action"] == "approve" else "Denied")
            ),
            parse_mode="HTML",
        )
        if self.manager.clock() < task["cleanup_at"]:
            return
        for name in ("vote", "result"):
            ref = task["refs"].get(name)
            if ref:
                await self.ops.call(
                    task,
                    "delete_" + name,
                    "delete_message",
                    dict(chat_id=ref["chat_id"], message_id=ref["message_id"]),
                    optional=True,
                )
        task["phase"] = "done"
        await self.save()

    async def tick(self):
        task = self.task
        if task["phase"] == "preparing":
            await self.prepare()
        elif task["phase"] == "voting":
            if self.manager.clock() >= task["deadline"]:
                await self.settle()
            else:
                await self.voting_setup()
        elif task["phase"] == "resolving":
            await self.resolve()
        elif task["phase"] == "cleanup":
            await self.cleanup()
        elif task["phase"] == "needs_attention":
            await self.ops.recover_deactivated(task)
            if task.get("mode") == "poll":
                await self.clear_poll_markup()
            await self.edit(
                "attention_" + str(task.get("attention_generation", 0)),
                "intro",
                self.text(
                    "recover_task",
                    uuid=task["uuid"],
                    user_id=task["user_id"],
                    reason=self.recovery_reason(),
                ),
                parse_mode="HTML",
                reply_markup=self.action_keyboard(recovery=True),
            )

    async def handle_vote(self, call, option, received_at):
        if option not in {"yes", "no"}:
            return await self.manager.answer(call, self.text("recover_expired"))
        member = await self.bot.get_chat_member(
            self.task["group_id"], call.from_user.id
        )
        if not member_present(member):
            return await self.manager.answer(
                call, self.text("insufficient_permissions")
            )
        result = await self.store.vote(
            self.task["uuid"],
            call.from_user.id,
            call.from_user.full_name,
            option,
            received_at,
        )
        key = {
            "expired": "recover_expired",
            "recorded": "jr_vote_recorded",
            "duplicate": "jr_already_voted",
        }[result]
        await self.manager.answer(call, self.text(key))

    async def handle_action(self, call, action):
        task = self.task
        if (
            not call.message
            or call.message.chat.id != task["group_id"]
            or not await self.manager.is_admin(task["group_id"], call.from_user.id)
        ):
            return await self.manager.answer(
                call, self.text("insufficient_permissions")
            )
        if action == "retry":
            if not await self.manager.retry(task):
                return await self.manager.answer(
                    call, self.text("recover_reason_snapshot"), show_alert=True
                )
        elif action in {"approve", "reject", "ban"}:
            decision = dict(
                action=action,
                admin_id=call.from_user.id,
                admin_name=call.from_user.full_name,
                reason="admin",
                generation=self.manager.update_id,
            )
            if not await self.store.decide(
                task["uuid"], decision, self.manager.clock(), manual=True
            ):
                return await self.manager.answer(call, self.text("recover_expired"))
        else:
            return await self.manager.answer(call, self.text("recover_expired"))
        await self.manager.answer(call, self.text("jr_status_processing_label"))

    async def handle_status_query(self, call):
        if call.from_user.id != self.task["user_id"]:
            return await self.manager.answer(
                call, self.text("insufficient_permissions")
            )
        phase = self.task["phase"]
        if self.task.get("applied"):
            label = (
                "jr_status_deactivated_label"
                if self.task["decision"].get("evidence") == "user_deactivated"
                else "jr_status_approve_label"
                if self.task["decision"]["action"] == "approve"
                else (
                    "jr_status_closed_label"
                    if self.task["decision"]["action"] == "closed"
                    else "jr_status_reject_label"
                )
            )
        elif phase == "needs_attention":
            label = "jr_status_attention_label"
        elif phase == "resolving":
            label = "jr_status_processing_label"
        else:
            label = "jr_status_pending_label"
        await self.manager.answer(
            call, self.text("jr_status_query", status=self.text(label)), show_alert=True
        )

    async def handle_realtime_result_request(self, message):
        if self.task["phase"] != "voting" or self.task["mode"] != "advanced":
            return await self.bot.reply_to(message, self.text("recover_expired"))
        if not await self.manager.is_member(
            self.task["group_id"], message.from_user.id
        ):
            return await self.bot.reply_to(
                message, self.text("insufficient_permissions")
            )
        votes = await self.store.votes(self.task["uuid"])
        if not any(row["user_id"] == message.from_user.id for row in votes):
            return await self.bot.reply_to(message, self.text("jr_not_voted"))
        yes = [row["name"] for row in votes if row["option"] == "yes"]
        no = [row["name"] for row in votes if row["option"] == "no"]
        text = self.text(
            "jr_live_votes_anonymous"
            if self.task["settings"].get("anonymous_vote", True)
            else "jr_live_votes_public",
            yes_votes=len(yes),
            no_votes=len(no),
            yes_names="\n".join(yes) or "-",
            no_names="\n".join(no) or "-",
        )
        await self.bot.reply_to(message, text)
