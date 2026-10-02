"""Durable polling and task scheduling for a single running bot instance."""

import asyncio
import json
import time
from uuid import NAMESPACE_URL, uuid5

from loguru import logger
from telebot import types, util, asyncio_helper

from app.join_request_vote import JoinRequestVote, member_present
from app.recovery_operations import (
    Deferred,
    OperationRunner,
    error_summary,
    poll_snapshot,
)
from utils.i18n import t


class PropagateHandlerErrors:
    def handle(self, exception):
        # AsyncTeleBot otherwise logs and swallows handler exceptions, which is
        # incompatible with acknowledging a durable inbox item after dispatch.
        raise exception


class RecoveryManager:
    def __init__(self, bot, database, config, clock=time.time):
        self.bot = bot
        self.database = database
        self.store = database.recovery
        self.config = config
        self.clock = clock
        self.ops = OperationRunner(bot, self.store, clock)
        self.username = ""
        self.bot_id = str(getattr(bot, "bot_id", "bot"))
        self.update_id = 0
        self.received_at = 0
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.stopping = False
        self.last_prune = 0
        self.ingesting = False

    async def instance(self, uuid):
        task = await self.store.get(uuid)
        return JoinRequestVote(self, task) if task else None

    async def answer(self, call, text, **kwargs):
        # Callback acknowledgements expire on Telegram. Their failure must not
        # undo a committed vote/decision or cause an update to be retried forever.
        try:
            await self.bot.answer_callback_query(call.id, text=text, **kwargs)
        except Exception:
            logger.debug(
                "Callback acknowledgement unavailable update={}", self.update_id
            )

    async def is_admin(self, chat_id, user_id):
        member = await self.bot.get_chat_member(chat_id, user_id)
        return member.status == "creator" or (
            member.status == "administrator"
            and getattr(member, "can_invite_users", False)
        )

    async def is_member(self, chat_id, user_id):
        return member_present(await self.bot.get_chat_member(chat_id, user_id))

    async def create(self, request):
        settings = await self.database.get_group_settings(request.chat.id)
        if not settings.get("vote_to_join", True):
            return
        uuid = str(
            uuid5(NAMESPACE_URL, f"approvebypoll:{self.bot_id}:{self.update_id}")
        )
        task = dict(
            version=1,
            uuid=uuid,
            group_id=request.chat.id,
            user_id=request.from_user.id,
            user_chat_id=getattr(request, "user_chat_id", request.from_user.id),
            chat_title=request.chat.title,
            applicant=dict(
                name=request.from_user.full_name, username=request.from_user.username
            ),
            settings=settings,
            phase="preparing",
            mode="advanced" if settings.get("advanced_vote") else "poll",
            refs={},
            created_at=self.received_at,
            request_date=request.date,
            source_update=self.update_id,
        )
        if self.config.get("logchannel.enable", False):
            task["log"] = dict(
                chat_id=int(self.config.get("logchannel.channel_id")),
                thread_id=int(self.config.get("logchannel.message_thread_id", 0)),
            )
        await self.store.create(task, self.clock())
        self.wakeup.set()

    async def on_poll(self, poll):
        if len(poll.options) >= 2:
            await self.store.poll(
                poll.id, poll_snapshot(poll), self.update_id, self.clock()
            )

    async def on_chat_member(self, update):
        if member_present(update.new_chat_member) and not member_present(
            update.old_chat_member
        ):
            await self.store.member_joined(
                update.chat.id,
                update.new_chat_member.user.id,
                update.date,
                self.update_id,
                self.clock(),
            )
            self.wakeup.set()

    async def retry(self, task):
        if task["phase"] != "needs_attention" or task.get("legacy"):
            return False
        # Never reset unknown send results to pending. A human retry must not
        # silently create a duplicate native/advanced poll.
        async with self.store.transaction() as c:
            rows = await c.rows(
                "SELECT name, data FROM recovery_operation WHERE uuid = ?", task["uuid"]
            )
            for row in rows:
                op = json.loads(row["data"])
                op.update(attempts=0, next_at=0)
                await c.execute(
                    "UPDATE recovery_operation SET data = ? WHERE uuid = ? AND name = ?",
                    json.dumps(op),
                    task["uuid"],
                    row["name"],
                )
            task.update(
                phase=task.get("resume_phase", "preparing"),
                error=None,
                poll_probe_attempts=0,
                poll_probe_next=0,
                attention_generation=task.get("attention_generation", 0) + 1,
            )
            await self.store._save(c, task, self.clock())
        return True

    async def recover_command(self, message):
        if (
            message.chat.type not in {"group", "supergroup"}
            or not message.from_user
            or getattr(message, "sender_chat", None)
        ):
            return
        settings = await self.database.get_group_settings(message.chat.id)
        lang = settings.get("language")
        if not await self.is_admin(message.chat.id, message.from_user.id):
            await self.bot.reply_to(message, t(lang, "insufficient_permissions"))
            return
        tasks = await self.store.tasks(message.chat.id, attention_only=True)
        if not tasks:
            await self.bot.reply_to(message, t(lang, "recover_empty"))
            return
        for task in tasks:
            if not task.get("settings"):
                task["settings"] = settings
                await self.store.save(task, self.clock())
            instance = JoinRequestVote(self, task)
            text = instance.text(
                "recover_task",
                uuid=task["uuid"],
                user_id=task["user_id"],
                reason=instance.recovery_reason(),
            )
            result = await instance.edit(
                f"recovery_refresh_{self.update_id}",
                "intro",
                text,
                reply_markup=instance.action_keyboard(recovery=True),
            )
            if result is None:
                ref = await instance.send(
                    f"recovery_card_{self.update_id}",
                    message.chat.id,
                    text,
                    reply_markup=instance.action_keyboard(recovery=True),
                )
                if ref:
                    task["refs"]["intro"] = ref
                    await self.store.save(task, self.clock())

    async def process_inbox(self):
        rows = await self.store.pending(self.clock())
        for row in rows:
            self.update_id = row["update_id"]
            self.received_at = row["received_at"]
            body = {}
            try:
                body = json.loads(row["body"])
                await self.bot.process_new_updates([types.Update.de_json(body)])
            except Deferred:
                async with self.store.transaction() as c:
                    await c.execute(
                        "UPDATE recovery_inbox SET next_at = ? WHERE update_id = ?",
                        self.clock() + 2,
                        row["update_id"],
                    )
                break
            except Exception as error:
                # A broken DB must stop scheduling; do not pretend failure state
                # was persisted if fail_update itself raises.
                exhausted = await self.store.fail_update(
                    row, error_summary(error), self.clock()
                )
                if exhausted:
                    callback = body.get("callback_query", {}).get("data", "").split()
                    if len(callback) >= 2 and callback[0] in {"jr", "jrv", "jrs"}:
                        await self.store.attention(
                            callback[1], "update_processing_failed", self.clock()
                        )
                    logger.error(
                        "Inbox update requires attention id={} error={}",
                        row["update_id"],
                        error_summary(error),
                    )
                # Preserve ordering until this update is handled or isolated.
                break
            else:
                await self.store.complete_update(row["update_id"], self.clock())
        return bool(rows)

    async def tick(self):
        for task in await self.store.tasks():
            if self.ingesting:
                break
            try:
                await JoinRequestVote(self, task).tick()
            except Deferred:
                continue
            except (KeyError, TypeError, ValueError) as error:
                await self.store.attention(
                    task["uuid"], "invalid_task: " + error_summary(error), self.clock()
                )
            # Unexpected/database errors propagate. The supervisor stops the bot
            # rather than making decisions using an uncommitted state.
        if self.clock() - self.last_prune >= 3600:
            await self.store.prune(self.clock())
            self.last_prune = self.clock()

    async def fetch(self, timeout=20):
        offset = await self.store.offset()
        updates = await asyncio_helper.get_updates(
            self.bot.token,
            offset=offset,
            timeout=timeout,
            allowed_updates=util.update_types,
        )
        if updates:
            self.ingesting = True
            # While receipt is being committed, a scheduler must not settle an
            # expired task ahead of an already-received qualifying vote.
            await self.store.receive(updates, self.clock())
            self.ingesting = False
            self.wakeup.set()
        return len(updates)

    async def receiver(self):
        attempts = 0
        while not self.stopping:
            try:
                await self.fetch()
                attempts = 0
            except Exception as error:
                # Only transport/API errors may retry here. Persistence failures
                # must propagate to the supervisor and stop acknowledgement.
                if (
                    not isinstance(error, (OSError, asyncio.TimeoutError))
                    and not hasattr(error, "error_code")
                    and type(error).__module__.split(".")[0]
                    not in {"aiohttp", "telebot"}
                ):
                    raise
                if getattr(error, "error_code", None) in {401, 409}:
                    raise
                attempts += 1
                logger.warning("Update reception retry: {}", error_summary(error))
                retry_after = (
                    getattr(error, "result_json", {})
                    .get("parameters", {})
                    .get("retry_after", 0)
                )
                await asyncio.sleep(max(min(60, 2 ** min(attempts, 6)), retry_after))

    async def worker(self):
        while not self.stopping:
            self.wakeup.clear()
            async with self.lock:
                await self.process_inbox()
                await self.tick()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        try:
            await self._run()
        finally:
            await self.bot.close_session()

    async def _run(self):
        await self.store.bind_bot(self.bot_id)
        await self.store.migrate_legacy(self.clock())
        tasks = await self.store.tasks()
        logger.info("Recovery loaded {} unfinished requests", len(tasks))
        # Replay local work, then fetch the remote backlog before any deadline
        # settlement. Newly fetched late button votes keep their actual receipt time.
        async with self.lock:
            while await self.process_inbox():
                if not await self.store.pending(self.clock()):
                    break
            while await self.fetch(timeout=0):
                await self.process_inbox()
        receiver = asyncio.create_task(self.receiver(), name="telegram-receiver")
        worker = asyncio.create_task(self.worker(), name="recovery-worker")
        try:
            done, _ = await asyncio.wait(
                [receiver, worker], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            self.stopping = True
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            # Give in-progress DB commits/API calls time to finish; persistent
            # in_flight markers cover cancellation at the shutdown deadline.
            self.wakeup.set()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
