import asyncio
import os
import sys
import json
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from telebot import types

from app.recovery import RecoveryManager, PropagateHandlerErrors
from tests import test_database


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class APIError(Exception):
    def __init__(self, code, description, retry_after=0):
        self.error_code = code
        self.description = description
        self.result_json = {"parameters": {"retry_after": retry_after}}
        super().__init__(description)


class FakeBot:
    bot_id = 42
    token = "42:fake"

    def __init__(self):
        self.calls = []
        self.failures = {}
        self.members = {9876543210: "left"}
        self.next_message = 100
        self.manager = None
        self.closed = False

    def count(self, method):
        return sum(name == method for name, _ in self.calls)

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(("get_chat_member", dict(chat_id=chat_id, user_id=user_id)))
        failures = self.failures.get("get_chat_member", [])
        if failures:
            raise failures.pop(0)
        return SimpleNamespace(
            status=self.members.get(user_id, "member"),
            can_invite_users=True,
            is_member=True,
        )

    async def process_new_updates(self, updates):
        for update in updates:
            if update.chat_join_request:
                await self.manager.create(update.chat_join_request)
            if update.poll:
                await self.manager.on_poll(update.poll)
            if update.chat_member:
                await self.manager.on_chat_member(update.chat_member)
            if update.callback_query:
                call = update.callback_query
                kind, uuid, *args = call.data.split()
                instance = await self.manager.instance(uuid)
                if kind == "jrv":
                    await instance.handle_vote(call, args[0], self.manager.received_at)
                elif kind == "jr":
                    await instance.handle_action(call, args[0])

    async def close_session(self):
        self.closed = True

    def __getattr__(self, method):
        async def call(**kwargs):
            self.calls.append((method, kwargs))
            failures = self.failures.get(method, [])
            if failures:
                raise failures.pop(0)
            if method in {"send_message", "send_poll", "reply_to"}:
                self.next_message += 1
                result = SimpleNamespace(
                    message_id=self.next_message,
                    chat=SimpleNamespace(id=kwargs.get("chat_id", -100)),
                    poll=None,
                )
                if method == "send_poll":
                    result.poll = SimpleNamespace(
                        id="poll-" + str(self.next_message),
                        is_closed=False,
                        options=[
                            SimpleNamespace(voter_count=0),
                            SimpleNamespace(voter_count=0),
                        ],
                    )
                return result
            if method == "stop_poll":
                return SimpleNamespace(
                    is_closed=True,
                    options=[
                        SimpleNamespace(voter_count=4),
                        SimpleNamespace(voter_count=1),
                    ],
                )
            if method == "approve_chat_join_request":
                self.members[kwargs["user_id"]] = "member"
            if method == "ban_chat_member":
                self.members[kwargs["user_id"]] = "kicked"
            return True

        return call

    async def answer_callback_query(self, callback_query_id, **kwargs):
        self.calls.append(("answer_callback_query", kwargs))


def request():
    return types.ChatJoinRequest.de_json(
        dict(
            chat=dict(id=-1001234567890, type="supergroup", title="Test group"),
            **{"from": dict(id=9876543210, is_bot=False, first_name="Applicant")},
            user_chat_id=9876543211,
            date=1000,
        )
    )


def vote_update(update_id, uuid, user_id=77, option="yes"):
    return dict(
        update_id=update_id,
        callback_query=dict(
            id=str(update_id),
            **{"from": dict(id=user_id, is_bot=False, first_name="Voter")},
            chat_instance="x",
            data=f"jrv {uuid} {option}",
            message=dict(
                message_id=102,
                date=1000,
                chat=dict(id=-1001234567890, type="supergroup"),
            ),
        ),
    )


class RecoveryContract:
    async def setup_manager(self):
        self.clock = Clock()
        self.bot = FakeBot()
        self.manager = RecoveryManager(self.bot, self.db, {}, self.clock)
        self.manager.username = "test_bot"
        self.bot.manager = self.manager
        self.store = self.db.recovery

    async def new_task(self, advanced=True):
        self.bot.members[request().from_user.id] = "left"
        await self.db.update_group_setting(request().chat.id, "advanced_vote", advanced)
        await self.db.update_group_setting(request().chat.id, "vote_time", 30)
        await self.db.update_group_setting(request().chat.id, "mini_voters", 1)
        self.manager.update_id = 1
        self.manager.received_at = self.clock()
        await self.manager.create(request())
        task = (await self.store.tasks())[0]
        await self.manager.tick()
        return await self.store.get(task["uuid"])

    async def reboot(self):
        await self.db.close()
        await self.db.connect()
        self.store = self.db.recovery
        self.manager = RecoveryManager(self.bot, self.db, {}, self.clock)
        self.bot.manager = self.manager

    async def test_month_long_votes_keep_original_deadline_across_restart(self):
        for index, advanced in enumerate((False, True), start=1):
            with self.subTest(advanced=advanced):
                await self.db.update_group_setting(
                    request().chat.id, "advanced_vote", advanced
                )
                await self.db.update_group_setting(
                    request().chat.id, "vote_time", 2592000
                )
                await self.db.update_group_setting(request().chat.id, "mini_voters", 1)
                self.manager.update_id = index
                self.manager.received_at = self.clock()
                self.bot.members[request().from_user.id] = "left"
                await self.manager.create(request())
                task = next(
                    task
                    for task in await self.store.tasks()
                    if task["phase"] == "preparing"
                )
                await self.manager.tick()
                await self.manager.tick()  # Finish pin/applicant notification setup.
                task = await self.store.get(task["uuid"])
                deadline = self.clock() + 2592000
                self.assertEqual(task["deadline"], deadline)
                if advanced:
                    await self.store.vote(
                        task["uuid"], 77, "Voter", "yes", deadline - 1
                    )
                else:
                    params = next(
                        params
                        for method, params in reversed(self.bot.calls)
                        if method == "send_poll"
                    )
                    self.assertEqual(params["close_date"], deadline)
                refs = task["refs"].copy()
                sends = self.bot.count("send_poll") + self.bot.count("send_message")
                await self.db.update_group_setting(task["group_id"], "vote_time", 60)
                await self.reboot()
                self.clock.now = deadline - 1
                await self.manager.tick()
                task = await self.store.get(task["uuid"])
                self.assertEqual(task["phase"], "voting")
                self.assertEqual(task["deadline"], deadline)
                self.assertEqual(task["refs"], refs)
                self.assertEqual(
                    self.bot.count("send_poll") + self.bot.count("send_message"), sends
                )
                self.clock.now = deadline
                await self.manager.tick()
                await self.manager.tick()
                self.assertIs(
                    (await self.db.get_join_request_status_by_uuid(task["uuid"]))[
                        "result"
                    ],
                    True,
                )
                self.clock.now += 60
                await self.manager.tick()
                self.assertEqual((await self.store.get(task["uuid"]))["phase"], "done")

    async def test_advanced_votes_deadline_settings_and_cleanup_survive(self):
        task = await self.new_task()
        uuid = task["uuid"]
        await self.manager.tick()  # pin/private setup
        self.assertEqual(
            await self.store.vote(uuid, 77, "Alice", "yes", 1010), "recorded"
        )
        await self.db.update_group_setting(task["group_id"], "vote_time", 900)
        sends = self.bot.count("send_message")
        await self.reboot()
        self.clock.now = 1020
        await self.manager.tick()
        task = await self.store.get(uuid)
        self.assertEqual(task["deadline"], 1030)
        self.assertEqual(task["settings"]["vote_time"], 30)
        self.assertEqual(self.bot.count("send_message"), sends)
        self.assertEqual(
            await self.store.vote(uuid, 77, "Alice", "no", 1020), "duplicate"
        )
        self.assertEqual(await self.store.vote(uuid, 78, "Bob", "yes", 1030), "expired")
        self.clock.now = 1031
        await self.manager.tick()
        self.assertEqual((await self.store.get(uuid))["phase"], "resolving")
        self.assertTrue(
            (await self.db.get_join_request_status_by_uuid(uuid))["waiting"]
        )
        await self.manager.tick()
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(uuid))["result"], True
        )
        await self.manager.tick()
        self.assertEqual(self.bot.count("approve_chat_join_request"), 1)
        self.assertEqual(self.bot.count("delete_message"), 0)
        sends = self.bot.count("send_message")
        await self.reboot()
        self.clock.now = 1200
        await self.manager.tick()
        self.assertEqual(self.bot.count("send_message"), sends)
        self.assertEqual(self.bot.count("delete_message"), 2)
        self.assertEqual((await self.store.get(uuid))["phase"], "done")

    async def test_original_poll_is_reused_and_only_final_snapshot_settles(self):
        task = await self.new_task(advanced=False)
        self.assertEqual(task["mode"], "poll")
        params = next(
            params for method, params in self.bot.calls if method == "send_poll"
        )
        self.assertEqual(params["close_date"], 1030)
        await self.store.poll(
            task["poll_id"], dict(closed=False, yes=99, no=0), 2, self.clock()
        )
        await self.reboot()
        self.assertFalse(await self.store.decide(task["uuid"], None, 1031))
        await self.store.poll(
            task["poll_id"], dict(closed=True, yes=1, no=2), 3, self.clock()
        )
        await self.store.poll(
            task["poll_id"], dict(closed=False, yes=99, no=0), 4, self.clock()
        )
        self.clock.now = 1040
        await self.manager.tick()
        state = await self.store.get(task["uuid"])
        self.assertEqual(state["decision"]["action"], "reject")
        self.assertEqual(state["decision"]["no"], 2)
        self.assertEqual(self.bot.count("send_poll"), 1)
        self.assertEqual(self.bot.count("stop_poll"), 0)

    async def test_inbox_receipt_time_duplicate_and_settlement_barrier(self):
        task = await self.new_task()
        body = vote_update(10, task["uuid"])
        await self.store.receive([body], 1029)
        await self.store.receive([body], 1035)
        self.clock.now = 1040
        self.assertFalse(await self.store.decide(task["uuid"], None, self.clock()))
        await self.reboot()
        self.assertEqual(await self.store.offset(), 11)
        await self.manager.process_inbox()
        votes = await self.store.votes(task["uuid"])
        self.assertEqual(len(votes), 1)
        self.assertEqual(votes[0]["received_at"], 1029)
        await self.store.receive([vote_update(11, task["uuid"], 88)], 1040)
        await self.manager.process_inbox()
        self.assertEqual(len(await self.store.votes(task["uuid"])), 1)
        await self.manager.tick()
        self.assertEqual((await self.store.get(task["uuid"]))["decision"]["yes"], 1)

    async def test_only_one_admin_or_timeout_decision(self):
        task = await self.new_task()
        self.clock.now = 1040
        results = await asyncio.gather(
            self.store.decide(
                task["uuid"],
                dict(action="approve", admin_id=9),
                self.clock(),
                manual=True,
            ),
            self.store.decide(task["uuid"], None, self.clock()),
        )
        self.assertEqual(sum(results), 1)
        await self.manager.tick()
        total = self.bot.count("approve_chat_join_request") + self.bot.count(
            "decline_chat_join_request"
        )
        self.assertEqual(total, 1)

    async def test_send_crash_window_never_repeats_poll(self):
        original = self.store.save_operation

        async def fail_after_send(uuid, name, op):
            if name == "advanced_poll" and op["state"] == "success":
                raise RuntimeError("simulated crash before commit")
            await original(uuid, name, op)

        with patch.object(self.store, "save_operation", side_effect=fail_after_send):
            with self.assertRaises(RuntimeError):
                await self.new_task()
        sends = self.bot.count("send_message")
        await self.reboot()
        await self.manager.tick()
        task = (await self.store.tasks())[0]
        self.assertEqual(task["phase"], "needs_attention")
        self.assertEqual(task["error"], "uncertain_advanced_poll")
        self.assertEqual(self.bot.count("send_message"), sends)
        await self.manager.retry(task)
        await self.manager.tick()
        self.assertEqual(self.bot.count("send_message"), sends)

    async def test_approval_crash_reconciles_without_second_approval(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="approve", admin_id=9), self.clock(), manual=True
        )
        original = self.store.save_operation

        async def fail_after_approval(uuid, name, op):
            if name == "apply_0" and op["state"] == "success":
                raise RuntimeError("simulated crash")
            await original(uuid, name, op)

        with patch.object(
            self.store, "save_operation", side_effect=fail_after_approval
        ):
            with self.assertRaises(RuntimeError):
                await self.manager.tick()
        self.assertTrue(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["waiting"]
        )
        await self.reboot()
        await self.manager.tick()
        self.assertEqual(self.bot.count("approve_chat_join_request"), 1)
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["result"],
            True,
        )

    async def test_uncertain_decline_is_not_reported_successful(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="reject", admin_id=9), self.clock(), manual=True
        )
        self.bot.failures["decline_chat_join_request"] = [TimeoutError()]
        await self.manager.tick()
        self.assertEqual(
            (await self.store.get(task["uuid"]))["phase"], "needs_attention"
        )
        self.assertTrue(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["waiting"]
        )
        self.assertIsNone(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["result"]
        )

    async def test_external_join_replayed_after_restart_closes_vote(self):
        task = await self.new_task(advanced=False)
        update = dict(
            update_id=2,
            chat_member=dict(
                chat=dict(id=request().chat.id, type="supergroup", title="Test group"),
                **{"from": dict(id=9, is_bot=False, first_name="Admin")},
                date=1001,
                old_chat_member=dict(
                    status="left",
                    user=dict(
                        id=request().from_user.id, is_bot=False, first_name="Applicant"
                    ),
                ),
                new_chat_member=dict(
                    status="member",
                    user=dict(
                        id=request().from_user.id, is_bot=False, first_name="Applicant"
                    ),
                ),
                via_join_request=True,
            ),
        )
        await self.store.receive([update], self.clock())
        await self.reboot()
        await self.manager.process_inbox()
        state = await self.store.get(task["uuid"])
        self.assertEqual(state["phase"], "cleanup")
        self.assertEqual(state["decision"]["reason"], "external")
        await self.manager.on_chat_member(
            types.ChatMemberUpdated.de_json(update["chat_member"])
        )
        await self.manager.tick()
        await self.reboot()
        await self.manager.tick()
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "done")
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["result"],
            True,
        )
        self.assertEqual(self.bot.count("approve_chat_join_request"), 0)
        self.assertEqual(self.bot.count("decline_chat_join_request"), 0)
        self.assertEqual(self.bot.count("send_poll"), 1)

    async def test_missed_external_approval_does_not_require_poll_totals(self):
        task = await self.new_task(advanced=False)
        self.bot.members[task["user_id"]] = "member"
        self.clock.now = task["deadline"]
        await self.reboot()
        await self.manager.tick()
        state = await self.store.get(task["uuid"])
        self.assertEqual(state["phase"], "cleanup")
        self.assertEqual(state["decision"]["reason"], "external")
        self.assertEqual(self.bot.count("stop_poll"), 0)
        self.assertEqual(self.bot.count("approve_chat_join_request"), 0)

    async def test_missing_request_closes_without_inventing_a_rejection(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="reject", admin_id=9), self.clock(), manual=True
        )
        self.bot.failures["decline_chat_join_request"] = [
            APIError(400, "Bad Request: HIDE_REQUESTER_MISSING")
        ]
        await self.manager.tick()
        state = await self.store.get(task["uuid"])
        record = await self.db.get_join_request_status_by_uuid(task["uuid"])
        self.assertFalse(record["waiting"])
        self.assertIsNone(record["result"])
        self.assertEqual(state["decision"]["action"], "closed")
        self.assertEqual(state["superseded_decision"]["action"], "reject")
        await self.reboot()
        await self.manager.tick()
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "done")
        self.assertEqual(self.bot.count("decline_chat_join_request"), 1)
        call = SimpleNamespace(
            id="status", from_user=SimpleNamespace(id=task["user_id"])
        )
        await (await self.manager.instance(task["uuid"])).handle_status_query(call)
        self.assertIn("Closed", self.bot.calls[-1][1]["text"])

    async def test_missing_request_evidence_survives_lookup_failure(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="approve", admin_id=9), self.clock(), manual=True
        )
        self.bot.failures["approve_chat_join_request"] = [
            APIError(400, "HIDE_REQUESTER_MISSING")
        ]
        self.bot.failures["get_chat_member"] = [TimeoutError()]
        await self.manager.tick()
        self.assertTrue(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["waiting"]
        )
        await self.reboot()
        self.clock.now += 3
        await self.manager.tick()
        self.assertFalse(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["waiting"]
        )
        self.assertEqual(self.bot.count("approve_chat_join_request"), 1)

    async def test_external_approval_wins_over_rejected_decision(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="reject", admin_id=9), self.clock(), manual=True
        )
        self.bot.members[task["user_id"]] = "member"
        self.bot.failures["decline_chat_join_request"] = [
            APIError(400, "HIDE_REQUESTER_MISSING")
        ]
        await self.manager.tick()
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["result"],
            True,
        )
        await self.store.finish_approval(task["uuid"], self.clock())
        self.assertEqual(
            (await self.store.get(task["uuid"]))["decision"]["reason"], "external"
        )

    async def test_arbitrary_api_error_does_not_prove_request_missing(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="reject", admin_id=9), self.clock(), manual=True
        )
        self.bot.failures["decline_chat_join_request"] = [
            APIError(400, "user not found")
        ]
        await self.manager.tick()
        self.assertTrue(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["waiting"]
        )
        self.assertEqual(
            (await self.store.get(task["uuid"]))["phase"], "needs_attention"
        )

    async def test_new_request_replaces_stale_request_and_old_events_cannot_close_it(
        self,
    ):
        old = await self.new_task()
        self.manager.update_id = 2
        await self.manager.create(
            request()
        )  # Same-date duplicate is not a new request.
        self.assertEqual(len(await self.store.tasks()), 1)
        new = request()
        new.date += 10
        self.manager.update_id = 3
        await self.manager.create(new)
        tasks = await self.store.tasks()
        self.assertEqual(len(tasks), 2)
        current = next(task for task in tasks if task["uuid"] != old["uuid"])
        self.assertEqual((await self.store.get(old["uuid"]))["phase"], "cleanup")
        self.assertFalse(
            (await self.db.get_join_request_status_by_uuid(old["uuid"]))["waiting"]
        )
        await self.store.member_joined(
            old["group_id"], old["user_id"], 1001, 2, self.clock()
        )
        await self.store.member_joined(
            old["group_id"], old["user_id"], 1001, 4, self.clock()
        )
        await self.store.finish_external(old["uuid"], "approve", "late", self.clock())
        self.assertEqual((await self.store.get(current["uuid"]))["phase"], "preparing")
        self.assertIsNone(
            (await self.db.get_join_request_status_by_uuid(old["uuid"]))["result"]
        )

    async def test_explicit_poll_rejection_falls_back_but_timeout_does_not(self):
        self.bot.failures["send_poll"] = [APIError(400, "polls unavailable")]
        task = await self.new_task(advanced=False)
        self.assertEqual(task["mode"], "advanced")
        self.assertEqual(task["phase"], "voting")

    async def test_poll_timeout_requires_attention(self):
        self.bot.failures["send_poll"] = [TimeoutError()]
        task = await self.new_task(advanced=False)
        self.assertEqual(task["phase"], "needs_attention")
        self.assertEqual(task["mode"], "poll")
        self.assertEqual(self.bot.count("send_message"), 1)

    async def test_retry_after_and_exhaustion_persist(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="approve", admin_id=9), self.clock(), manual=True
        )
        self.bot.failures["approve_chat_join_request"] = [
            APIError(429, "rate limited", retry_after=120)
        ] * 10
        await self.manager.tick()
        op = await self.store.operation(task["uuid"], "apply_0")
        self.assertEqual(op["next_at"], 1120)
        await self.reboot()
        await self.manager.tick()
        self.assertEqual(self.bot.count("approve_chat_join_request"), 1)
        for _ in range(9):
            self.clock.now += 120
            await self.manager.tick()
        self.assertEqual(
            (await self.store.operation(task["uuid"], "apply_0"))["attempts"], 10
        )
        self.assertEqual(
            (await self.store.get(task["uuid"]))["phase"], "needs_attention"
        )

    async def test_missing_final_poll_never_becomes_zero_votes(self):
        task = await self.new_task(advanced=False)
        self.bot.failures["stop_poll"] = [
            APIError(400, "poll has already been closed")
        ] * 10
        self.clock.now = 1040
        for _ in range(10):
            await self.manager.tick()
            self.clock.now += 60
        state = await self.store.get(task["uuid"])
        self.assertEqual(state["phase"], "needs_attention")
        self.assertNotIn("decision", state)
        self.assertEqual(self.bot.count("decline_chat_join_request"), 0)
        await self.store.poll(
            task["poll_id"], dict(closed=True, yes=3, no=0), 9, self.clock()
        )
        await self.manager.retry(await self.store.get(task["uuid"]))
        await self.manager.tick()
        self.assertEqual(
            (await self.store.get(task["uuid"]))["decision"]["action"], "approve"
        )

    async def test_legacy_upgrade_and_admin_resolution(self):
        uid = str(uuid4())
        await self.db.create_join_request(uid, -100, 55)
        self.assertEqual(await self.store.migrate_legacy(self.clock()), 1)
        self.assertEqual(await self.store.migrate_legacy(self.clock()), 0)
        task = await self.store.get(uid)
        self.assertTrue(task["legacy"])
        await self.manager.tick()
        self.assertEqual(self.bot.count("send_poll"), 0)
        await self.store.decide(
            uid, dict(action="ban", admin_id=9), self.clock(), manual=True
        )
        await self.manager.tick()
        self.assertEqual(self.bot.count("ban_chat_member"), 1)
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(uid))["result"], False
        )

    async def test_inbox_failure_ordering_and_retention(self):
        await self.store.receive([dict(update_id=1), dict(update_id=2)], 1000)
        row = (await self.store.pending(1000))[0]
        await self.store.fail_update(row, "test", 1000)
        self.assertEqual(await self.store.pending(1001), [])
        self.assertEqual(
            [row["update_id"] for row in await self.store.pending(1002)], [1, 2]
        )
        await self.store.complete_update(1, 1002)
        await self.store.prune(1002 + 7 * 86400 + 1)
        async with self.store.transaction() as c:
            rows = await c.rows("SELECT * FROM recovery_inbox")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["update_id"], 2)

    async def test_creation_replay_does_not_duplicate(self):
        task = await self.new_task()
        await self.manager.create(request())
        self.manager.update_id = 2
        await self.manager.create(request())
        self.assertEqual(
            [row["uuid"] for row in await self.store.tasks()], [task["uuid"]]
        )

    async def test_inbox_batch_rollback_does_not_advance_offset(self):
        await self.store.receive([dict(update_id=10)], self.clock())
        with self.assertRaises(KeyError):
            await self.store.receive(
                [dict(update_id=11), dict(broken=True)], self.clock()
            )
        self.assertEqual(await self.store.offset(), 11)
        self.assertEqual(
            [row["update_id"] for row in await self.store.pending(self.clock())], [10]
        )

    async def test_handler_exception_is_not_swallowed(self):
        from telebot.async_telebot import AsyncTeleBot

        bot = AsyncTeleBot("42:fake", exception_handler=PropagateHandlerErrors())

        @bot.poll_handler(func=lambda poll: True)
        async def broken(poll):
            raise RuntimeError("failure")

        with self.assertRaises(RuntimeError):
            await bot.process_new_updates(
                [
                    types.Update.de_json(
                        dict(
                            update_id=1,
                            poll=dict(
                                id="p",
                                question="q",
                                options=[
                                    dict(text="yes", voter_count=0),
                                    dict(text="no", voter_count=0),
                                ],
                                total_voter_count=0,
                                is_closed=True,
                                is_anonymous=True,
                                type="regular",
                                allows_multiple_answers=False,
                            ),
                        )
                    )
                ]
            )

    async def test_unauthorized_and_wrong_group_admin_actions(self):
        task = await self.new_task()
        instance = await self.manager.instance(task["uuid"])
        call = SimpleNamespace(
            id="1",
            from_user=SimpleNamespace(id=99, full_name="Intruder"),
            message=SimpleNamespace(chat=SimpleNamespace(id=task["group_id"])),
        )
        await instance.handle_action(call, "approve")
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "voting")
        self.bot.members[99] = "administrator"
        call.message.chat.id = -999
        await instance.handle_action(call, "approve")
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "voting")

    async def test_graceful_shutdown_joins_receiver_and_worker(self):
        async def empty_updates(*args, **kwargs):
            await asyncio.sleep(0.01)
            return []

        with patch(
            "app.recovery.asyncio_helper.get_updates", side_effect=empty_updates
        ):
            running = asyncio.create_task(self.manager.run())
            await asyncio.sleep(0.05)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertTrue(self.bot.closed)
        names = {task.get_name() for task in asyncio.all_tasks() if not task.done()}
        self.assertNotIn("telegram-receiver", names)
        self.assertNotIn("recovery-worker", names)

    async def test_database_failure_stops_receiver_without_telegram_acknowledgement(
        self,
    ):
        from unittest.mock import AsyncMock

        fetch = AsyncMock(return_value=[dict(update_id=10)])
        with patch("app.recovery.asyncio_helper.get_updates", fetch):
            with patch.object(
                self.store, "receive", side_effect=RuntimeError("disk failure")
            ):
                with self.assertRaises(RuntimeError):
                    await self.manager.receiver()
        self.assertEqual(fetch.await_count, 1)
        self.assertIsNone(await self.store.offset())
        self.assertTrue(self.manager.ingesting)

    async def test_auto_closed_poll_fallback_reads_authoritative_message(self):
        from unittest.mock import AsyncMock

        task = await self.new_task(advanced=False)
        self.bot.failures["stop_poll"] = [APIError(400, "poll can't be stopped")]
        message = SimpleNamespace(
            message_id=task["refs"]["vote"]["message_id"],
            chat=SimpleNamespace(id=task["group_id"]),
            poll=SimpleNamespace(
                id=task["poll_id"],
                is_closed=True,
                options=[
                    SimpleNamespace(voter_count=2),
                    SimpleNamespace(voter_count=3),
                ],
            ),
        )
        self.clock.now = task["deadline"] + 1
        with patch.object(
            self.bot, "edit_message_reply_markup", new=AsyncMock(return_value=message)
        ):
            await self.manager.tick()
        state = await self.store.get(task["uuid"])
        self.assertEqual(state["phase"], "resolving")
        self.assertEqual(state["decision"]["yes"], 2)
        self.assertEqual(state["decision"]["no"], 3)
        self.assertEqual(state["decision"]["action"], "reject")

    async def test_incomplete_poll_probe_retry_budget_resets_without_reusing_results(
        self,
    ):
        task = await self.new_task(advanced=False)
        self.bot.failures["stop_poll"] = [APIError(400, "poll can't be stopped")]
        self.clock.now = task["deadline"] + 1
        for _ in range(10):
            await self.manager.tick()
            self.clock.now += 60
        task = await self.store.get(task["uuid"])
        self.assertEqual(task["phase"], "needs_attention")
        self.assertEqual(task["poll_probe"], 10)
        await self.manager.retry(task)
        await self.manager.tick()
        task = await self.store.get(task["uuid"])
        self.assertEqual(task["poll_probe"], 11)
        self.assertEqual(task["poll_probe_attempts"], 1)
        self.assertIsNotNone(
            await self.store.operation(task["uuid"], "read_poll_controls_10")
        )

    async def test_recover_updates_original_or_replaces_deleted_message(self):
        task = await self.new_task()
        await self.store.attention(task["uuid"], "test_error", self.clock())
        self.bot.members[99] = "administrator"
        message = SimpleNamespace(
            chat=SimpleNamespace(id=task["group_id"], type="supergroup"),
            from_user=SimpleNamespace(id=99),
            sender_chat=None,
        )
        count = self.bot.count("send_message")
        await self.manager.recover_command(message)
        self.assertEqual(self.bot.count("send_message"), count)
        self.manager.update_id = 2
        self.bot.failures["edit_message_text"] = [
            APIError(400, "message to edit not found")
        ]
        await self.manager.recover_command(message)
        state = await self.store.get(task["uuid"])
        self.assertEqual(self.bot.count("send_message"), count + 1)
        self.assertNotEqual(state["refs"]["intro"], task["refs"]["intro"])
        # Same command after a crash must not emit another card.
        await self.manager.recover_command(message)
        self.assertEqual(self.bot.count("send_message"), count + 1)

    async def test_inbox_binds_to_one_bot_and_pauses_settlement_during_receipt(self):
        await self.store.bind_bot(42)
        await self.store.bind_bot(42)
        with self.assertRaises(ValueError):
            await self.store.bind_bot(43)
        task = await self.new_task()
        self.clock.now = task["deadline"] + 1
        self.manager.ingesting = True
        await self.manager.tick()
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "voting")
        self.manager.ingesting = False
        await self.manager.tick()
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "resolving")

    async def test_completed_send_and_poll_identity_commit_together(self):
        await self.db.update_group_setting(request().chat.id, "advanced_vote", False)
        self.manager.update_id = 1
        self.manager.received_at = self.clock()
        await self.manager.create(request())
        original = self.store.save

        async def interrupt_snapshot(task, now):
            if task.get("poll_id"):
                raise RuntimeError("crash after message result commit")
            await original(task, now)

        with patch.object(self.store, "save", side_effect=interrupt_snapshot):
            with self.assertRaises(RuntimeError):
                await self.manager.tick()
        await self.reboot()
        task = (await self.store.tasks())[0]
        self.assertIn("poll_id", task)
        self.assertIn("vote", task["refs"])
        self.assertTrue(
            await self.store.poll(
                task["poll_id"], dict(closed=True, yes=4, no=1), 3, self.clock()
            )
        )
        self.clock.now = task["deadline"] + 1
        await self.manager.tick()
        await self.manager.tick()
        self.assertEqual(self.bot.count("send_poll"), 1)
        self.assertEqual((await self.store.get(task["uuid"]))["decision"]["yes"], 4)

    async def test_permission_restored_allows_explicit_retry(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="approve", admin_id=9), self.clock(), manual=True
        )
        self.bot.failures["approve_chat_join_request"] = [
            APIError(403, "not enough rights")
        ]
        await self.manager.tick()
        task = await self.store.get(task["uuid"])
        self.assertEqual(task["phase"], "needs_attention")
        await self.manager.retry(task)
        await self.manager.tick()
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["result"],
            True,
        )

    async def test_fetch_saves_raw_update_before_advancing_offset(self):
        from unittest.mock import AsyncMock

        updates = [vote_update(10, str(uuid4()))]
        with patch(
            "app.recovery.asyncio_helper.get_updates",
            new=AsyncMock(return_value=updates),
        ) as fetch:
            with patch.object(
                self.store, "receive", side_effect=RuntimeError("disk unavailable")
            ):
                with self.assertRaises(RuntimeError):
                    await self.manager.fetch(timeout=0)
            self.assertIsNone(await self.store.offset())
            await self.manager.fetch(timeout=0)
            self.assertIsNone(fetch.call_args.kwargs["offset"])
            await self.manager.fetch(timeout=0)
            self.assertEqual(fetch.call_args.kwargs["offset"], 11)
        self.assertEqual(len(await self.store.pending(self.clock())), 1)

    async def test_deleted_cleanup_message_is_success(self):
        task = await self.new_task()
        await self.store.decide(
            task["uuid"], dict(action="approve", admin_id=9), self.clock(), manual=True
        )
        await self.manager.tick()
        self.bot.failures["delete_message"] = [
            APIError(400, "message to delete not found")
        ]
        await self.manager.tick()
        self.assertEqual((await self.store.get(task["uuid"]))["phase"], "done")
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(task["uuid"]))["result"],
            True,
        )

    async def test_sigkill_preserves_commits_and_rolls_back_open_transaction(self):
        # Each scenario uses a separate request id but the same real backend.
        for scenario in ("inbox", "vote", "transaction"):
            async with self.store.transaction() as c:
                for table in (
                    "recovery_vote",
                    "recovery_operation",
                    "recovery_inbox",
                    "recovery_task",
                    "join_request",
                    "recovery_meta",
                ):
                    await c.execute("DELETE FROM " + table)
            config = (
                {"backend": "postgresql", **self.db.config}
                if self.store.postgres
                else {"backend": "sqlite", "path": self.db.path}
            )
            with tempfile.TemporaryDirectory() as root:
                ready = Path(root) / "ready"
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tests.recovery_crash_worker",
                    str(ready),
                    scenario,
                    env={**os.environ, "RECOVERY_TEST_CONFIG": json.dumps(config)},
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    async with asyncio.timeout(15):
                        while not ready.exists():
                            if child.returncode is not None:
                                self.fail(f"Crash worker exited: {child.returncode}")
                            await asyncio.sleep(0.02)
                    uuid = ready.read_text()
                    child.kill()
                    await child.wait()
                    self.assertLess(child.returncode, 0)
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
            await self.reboot()
            self.clock.now = 1040
            await self.manager.process_inbox()
            votes = await self.store.votes(uuid)
            self.assertEqual(len(votes), 0 if scenario == "transaction" else 1)
            self.assertEqual((await self.store.get(uuid))["deadline"], 1030)
            await self.manager.tick()
            self.assertEqual(
                (await self.store.get(uuid))["decision"]["yes"], len(votes)
            )

    async def test_corrupt_snapshot_is_isolated(self):
        task = await self.new_task()
        async with self.store.transaction() as c:
            await c.execute(
                "UPDATE recovery_task SET data = ? WHERE uuid = ?",
                "{broken",
                task["uuid"],
            )
        tasks = await self.store.tasks()
        self.assertEqual(tasks[0]["phase"], "needs_attention")
        self.assertEqual(tasks[0]["error"], "corrupt_snapshot")


class SQLiteRecoveryTests(RecoveryContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_database.SQLiteTests.asyncSetUp(self)
        await self.setup_manager()


@unittest.skipUnless(
    os.environ.get("TEST_POSTGRES_DSN"),
    "Set TEST_POSTGRES_DSN for PostgreSQL recovery tests",
)
class PostgreSQLRecoveryTests(RecoveryContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_database.PostgreSQLTests.asyncSetUp(self)
        await self.setup_manager()
