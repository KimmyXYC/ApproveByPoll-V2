"""Persist Telegram operation intent before making externally visible changes."""

from loguru import logger


class Deferred(Exception):
    """Work is scheduled for retry or requires an administrator."""


class Rejected(Exception):
    """Telegram explicitly rejected a native poll; advanced fallback is safe."""


class PollUnavailable(Exception):
    """The poll cannot be stopped again; query its message for final totals."""


def error_summary(error):
    # Avoid logging URLs/token-bearing transport exception strings.
    code = getattr(error, "error_code", None)
    description = getattr(error, "description", "")
    return f"{type(error).__name__}: {code or 'transport'} {description}"[:500]


def poll_snapshot(poll):
    return dict(
        closed=bool(poll.is_closed),
        yes=int(poll.options[0].voter_count),
        no=int(poll.options[1].voter_count),
    )


def member_present(member):
    return member.status in {"member", "administrator", "creator"} or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )


def serialize_result(result):
    if hasattr(result, "status"):
        return dict(status=result.status, is_member=getattr(result, "is_member", False))
    if hasattr(result, "message_id"):
        value = dict(message_id=result.message_id, chat_id=result.chat.id)
        if getattr(result, "poll", None):
            value["poll_id"] = result.poll.id
            value["poll"] = poll_snapshot(result.poll)
        return value
    if hasattr(result, "options"):
        return poll_snapshot(result)
    return result


class OperationRunner:
    def __init__(self, bot, store, clock):
        self.bot = bot
        self.store = store
        self.clock = clock

    async def attention(self, task, reason):
        await self.store.attention(task["uuid"], reason, self.clock())
        logger.warning(
            "Recovery requires administrator uuid={} reason={}", task["uuid"], reason
        )
        raise Deferred

    async def reconcile(self, task, name, op):
        """Only confirm a verifiable desired state, never infer a rejection."""
        try:
            member = await self.bot.get_chat_member(task["group_id"], task["user_id"])
        except Exception as error:
            await self.retry(task, name, op, error, unknown=True)
        action = op["method"]
        if action in {"approve_chat_join_request", "decline_chat_join_request"} and (
            op.get("request_missing")
            or (action == "decline_chat_join_request" and member_present(member))
        ):
            await self.store.finish_external(
                task["uuid"],
                "approve" if member_present(member) else "closed",
                "request_missing" if op.get("request_missing") else "member_present",
                self.clock(),
            )
            raise Deferred
        confirmed = (
            action == "approve_chat_join_request"
            and (
                member.status in {"member", "administrator", "creator"}
                or (
                    member.status == "restricted"
                    and getattr(member, "is_member", False)
                )
            )
        ) or (action == "ban_chat_member" and member.status == "kicked")
        if confirmed:
            op.update(state="success", result=True, error=None)
            await self.store.save_operation(task["uuid"], name, op)
            return True
        await self.attention(task, f"uncertain_{name}")

    async def retry(self, task, name, op, error, unknown=False):
        op["attempts"] += 1
        params = getattr(error, "result_json", {}).get("parameters", {})
        delay = max(min(60, 2 ** min(op["attempts"], 6)), params.get("retry_after", 0))
        op.update(
            state="unknown" if unknown else "pending",
            next_at=self.clock() + delay,
            error=error_summary(error),
        )
        await self.store.save_operation(task["uuid"], name, op)
        if op["attempts"] >= 10:
            await self.attention(task, f"retries_exhausted_{name}: {op['error']}")
        raise Deferred

    async def call(
        self, task, name, method, params, policy="idempotent", optional=False
    ):
        op = await self.store.operation(task["uuid"], name)
        if op is None:
            op = dict(
                version=1,
                state="pending",
                attempts=0,
                next_at=0,
                method=method,
                params=params,
                policy=policy,
            )
            await self.store.save_operation(task["uuid"], name, op)
        if op["state"] == "success":
            return op.get("result")
        if op["state"] == "skipped":
            return None
        if op["state"] == "rejected":
            raise Rejected
        if op["state"] == "unavailable":
            raise PollUnavailable
        if op["next_at"] > self.clock():
            raise Deferred
        if op["state"] in {"in_flight", "unknown"}:
            if policy == "core_send":
                await self.attention(task, f"uncertain_{name}")
            if policy == "aux_send":
                op.update(state="skipped", error="delivery_unknown")
                await self.store.save_operation(task["uuid"], name, op)
                return None
            if policy == "approval":
                return await self.reconcile(task, name, op)
        op["state"] = "in_flight"
        await self.store.save_operation(task["uuid"], name, op)
        try:
            result = await getattr(self.bot, op["method"])(**op["params"])
        except Exception as error:
            code = getattr(error, "error_code", None)
            description = str(getattr(error, "description", "")).lower()
            if code == 429:
                await self.retry(task, name, op, error)
            if code == 400 and (
                (
                    method == "delete_message"
                    and "message to delete not found" in description
                )
                or (
                    method.startswith("edit_message")
                    and "message is not modified" in description
                )
                or (
                    method == "unpin_chat_message"
                    and "message to unpin not found" in description
                )
            ):
                result = True
            elif (
                method == "stop_poll"
                and code == 400
                and not optional
                and (
                    "closed" in description
                    or "not found" in description
                    or "can't be stopped" in description
                )
            ):
                op.update(state="unavailable", error=error_summary(error))
                await self.store.save_operation(task["uuid"], name, op)
                raise PollUnavailable from error
            elif method == "send_poll" and code == 400:
                op.update(state="rejected", error=error_summary(error))
                await self.store.save_operation(task["uuid"], name, op)
                raise Rejected from error
            elif code in {400, 401, 403}:
                if policy == "approval" and (
                    code in {401, 403}
                    or "rights" in description
                    or "permission" in description
                ):
                    op.update(state="pending", error=error_summary(error))
                    await self.store.save_operation(task["uuid"], name, op)
                    await self.attention(task, f"{name}: {op['error']}")
                if code == 400 and method in {
                    "approve_chat_join_request",
                    "decline_chat_join_request",
                }:
                    # Only this explicit Telegram error proves that there is no
                    # pending request. A left member or arbitrary 400 does not.
                    op["request_missing"] = "hide_requester_missing" in description
                op.update(state="unknown", error=error_summary(error))
                await self.store.save_operation(task["uuid"], name, op)
                if policy == "approval":
                    return await self.reconcile(task, name, op)
                if optional:
                    op["state"] = "skipped"
                    await self.store.save_operation(task["uuid"], name, op)
                    return None
                await self.attention(task, f"{name}: {op['error']}")
            elif policy in {"core_send", "aux_send", "approval"}:
                op.update(state="unknown", error=error_summary(error))
                await self.store.save_operation(task["uuid"], name, op)
                if policy == "approval":
                    return await self.reconcile(task, name, op)
                if policy == "aux_send":
                    op["state"] = "skipped"
                    await self.store.save_operation(task["uuid"], name, op)
                    return None
                await self.attention(task, f"uncertain_{name}")
            else:
                await self.retry(task, name, op, error)
        # A DB failure here deliberately leaves in_flight persisted. Recovery must
        # reconcile it; it must not report success or issue a second unsafe send.
        op.update(state="success", result=serialize_result(result), error=None)
        await self.store.save_operation(task["uuid"], name, op)
        return op["result"]
