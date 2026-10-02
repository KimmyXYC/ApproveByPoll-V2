"""Durable task state and inbox shared by PostgreSQL and SQLite.

All read/modify/write operations run in one transaction. JSON contains only data,
never pickled Python/SDK objects. Times are UTC Unix seconds.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from uuid import UUID


class ReentrantLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None
        self.depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self.owner is not task:
            await self.lock.acquire()
            self.owner = task
        self.depth += 1

    async def __aexit__(self, *args):
        self.depth -= 1
        if not self.depth:
            self.owner = None
            self.lock.release()


def serialized(method):
    async def wrapper(self, *args, **kwargs):
        async with self.transaction_lock:
            return await method(self, *args, **kwargs)

    return wrapper


class Connection:
    def __init__(self, connection, postgres):
        self.connection = connection
        self.postgres = postgres

    def sql(self, sql):
        if not self.postgres:
            return sql
        index = iter(range(1, sql.count("?") + 1))
        return re.sub(r"\?", lambda _: f"${next(index)}", sql)

    async def execute(self, sql, *args):
        if self.postgres:
            result = await self.connection.execute(self.sql(sql), *args)
            return (
                int(result.rsplit(" ", 1)[-1])
                if result.rsplit(" ", 1)[-1].isdigit()
                else 0
            )
        async with self.connection.execute(sql, args) as cursor:
            return cursor.rowcount

    async def rows(self, sql, *args):
        if self.postgres:
            return [
                dict(row) for row in await self.connection.fetch(self.sql(sql), *args)
            ]
        async with self.connection.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def row(self, sql, *args):
        rows = await self.rows(sql, *args)
        return rows[0] if rows else None


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS recovery_task (
        uuid TEXT PRIMARY KEY, group_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        phase TEXT NOT NULL, data TEXT NOT NULL, poll_id TEXT UNIQUE,
        updated_at DOUBLE PRECISION NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS recovery_task_phase ON recovery_task (phase, group_id)",
    """CREATE TABLE IF NOT EXISTS recovery_vote (
        uuid TEXT NOT NULL, user_id BIGINT NOT NULL, name TEXT NOT NULL,
        option TEXT NOT NULL CHECK (option IN ('yes', 'no')),
        received_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (uuid, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS recovery_operation (
        uuid TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY (uuid, name)
    )""",
    """CREATE TABLE IF NOT EXISTS recovery_inbox (
        update_id BIGINT PRIMARY KEY, body TEXT NOT NULL,
        received_at DOUBLE PRECISION NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        error TEXT, completed_at DOUBLE PRECISION
    )""",
    "CREATE INDEX IF NOT EXISTS recovery_inbox_state ON recovery_inbox (state, update_id)",
    """CREATE TABLE IF NOT EXISTS recovery_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""",
]


class RecoveryStore:
    def __init__(self, database, postgres=False):
        self.database = database
        self.postgres = postgres

    @asynccontextmanager
    async def transaction(self):
        if self.postgres:
            async with self.database.conn.acquire() as connection:
                async with connection.transaction():
                    await connection.execute("SET LOCAL synchronous_commit = on")
                    # One running bot is supported. Serialize recovery transactions,
                    # including first creation when no task row exists to lock yet.
                    await connection.execute("SELECT pg_advisory_xact_lock(734928162)")
                    yield Connection(connection, True)
        else:
            async with self.database.transaction_lock:
                connection = self.database.conn
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    yield Connection(connection, False)
                except BaseException:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()

    async def initialize(self):
        async with self.transaction() as c:
            for sql in SCHEMA:
                await c.execute(sql)

    @staticmethod
    def decode(row):
        if row is None:
            return None
        data = json.loads(row["data"])
        if not isinstance(data, dict):
            raise ValueError("Recovery snapshot must be an object")
        data.update(
            uuid=row["uuid"],
            group_id=row["group_id"],
            user_id=row["user_id"],
            phase=row["phase"],
        )
        return data

    async def _get(self, c, uuid):
        suffix = " FOR UPDATE" if self.postgres else ""
        return self.decode(
            await c.row("SELECT * FROM recovery_task WHERE uuid = ?" + suffix, uuid)
        )

    async def _save(self, c, task, now):
        await c.execute(
            "UPDATE recovery_task SET phase = ?, data = ?, poll_id = ?, updated_at = ? WHERE uuid = ?",
            task["phase"],
            json.dumps(task, ensure_ascii=False),
            task.get("poll_id"),
            now,
            task["uuid"],
        )

    async def get(self, uuid):
        async with self.transaction() as c:
            return await self._get(c, uuid)

    async def tasks(self, group_id=None, attention_only=False):
        async with self.transaction() as c:
            sql = "SELECT * FROM recovery_task WHERE phase " + (
                "= 'needs_attention'" if attention_only else "!= 'done'"
            )
            args = []
            if group_id is not None:
                sql += " AND group_id = ?"
                args.append(group_id)
            rows = await c.rows(sql + " ORDER BY updated_at, uuid", *args)
            tasks = []
            for row in rows:
                try:
                    task = self.decode(row)
                    if task.get("version") != 1:
                        raise ValueError("Unsupported recovery snapshot version")
                    if task["phase"] not in {
                        "preparing",
                        "voting",
                        "resolving",
                        "cleanup",
                        "needs_attention",
                        "done",
                    }:
                        raise ValueError("Unknown task phase")
                    if not isinstance(task.get("settings"), dict) or not isinstance(
                        task.get("refs"), dict
                    ):
                        raise ValueError("Invalid settings or message references")
                except (ValueError, TypeError, KeyError):
                    task = dict(
                        uuid=row["uuid"],
                        group_id=row["group_id"],
                        user_id=row["user_id"],
                        phase="needs_attention",
                        version=1,
                        resume_phase="preparing",
                        error="corrupt_snapshot",
                        legacy=True,
                        settings={},
                        refs={},
                    )
                    await self._save(c, task, row["updated_at"])
                tasks.append(task)
            return tasks

    async def save(self, task, now):
        async with self.transaction() as c:
            await self._save(c, task, now)

    async def create(self, task, now):
        async with self.transaction() as c:
            existing = await self._get(c, task["uuid"])
            if existing:
                return existing
            waiting = await c.row(
                "SELECT uuid FROM join_request WHERE group_id = ? AND user_id = ? AND waiting = TRUE",
                task["group_id"],
                task["user_id"],
            )
            if waiting:
                previous = await self._get(c, str(waiting["uuid"]))
                # A later Telegram request proves that the previous request is
                # no longer pending. Replayed/same-date updates are not proof.
                if (
                    not previous
                    or not previous.get("request_date")
                    or task.get("request_date", 0) <= previous["request_date"]
                    or task.get("source_update", 0) <= previous.get("source_update", 0)
                ):
                    return None
                await self._finish_external(c, previous, "closed", "new_request", now)
            uid = UUID(task["uuid"]) if self.postgres else task["uuid"]
            await c.execute(
                "INSERT INTO join_request (uuid, group_id, user_id, request_time, waiting) VALUES (?, ?, ?, CURRENT_TIMESTAMP, TRUE)",
                uid,
                task["group_id"],
                task["user_id"],
            )
            await c.execute(
                "INSERT INTO recovery_task (uuid, group_id, user_id, phase, data, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                task["uuid"],
                task["group_id"],
                task["user_id"],
                task["phase"],
                json.dumps(task, ensure_ascii=False),
                now,
            )
            return task

    async def migrate_legacy(self, now):
        async with self.transaction() as c:
            rows = await c.rows(
                "SELECT j.* FROM join_request j LEFT JOIN recovery_task t ON CAST(j.uuid AS TEXT) = t.uuid WHERE j.waiting = TRUE AND t.uuid IS NULL"
            )
            for row in rows:
                task = dict(
                    version=1,
                    uuid=str(row["uuid"]),
                    group_id=row["group_id"],
                    user_id=row["user_id"],
                    phase="needs_attention",
                    resume_phase="preparing",
                    error="legacy_snapshot_missing",
                    legacy=True,
                    settings={},
                    refs={},
                )
                await c.execute(
                    "INSERT INTO recovery_task (uuid, group_id, user_id, phase, data, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    task["uuid"],
                    task["group_id"],
                    task["user_id"],
                    task["phase"],
                    json.dumps(task),
                    now,
                )
            return len(rows)

    async def vote(self, uuid, user_id, name, option, received_at):
        async with self.transaction() as c:
            task = await self._get(c, uuid)
            if (
                not task
                or task["phase"] != "voting"
                or task.get("mode") != "advanced"
                or received_at >= task["deadline"]
            ):
                return "expired"
            count = await c.execute(
                "INSERT INTO recovery_vote (uuid, user_id, name, option, received_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (uuid, user_id) DO NOTHING",
                uuid,
                user_id,
                name,
                option,
                received_at,
            )
            return "recorded" if count == 1 else "duplicate"

    async def votes(self, uuid):
        async with self.transaction() as c:
            return await c.rows(
                "SELECT * FROM recovery_vote WHERE uuid = ? ORDER BY received_at, user_id",
                uuid,
            )

    async def decide(self, uuid, decision, now, manual=False):
        async with self.transaction() as c:
            task = await self._get(c, uuid)
            allowed = (
                {"voting", "needs_attention", "preparing"} if manual else {"voting"}
            )
            if not task or task["phase"] not in allowed or task.get("applied"):
                return False
            record = await c.row(
                "SELECT waiting FROM join_request WHERE CAST(uuid AS TEXT) = ?", uuid
            )
            if not record or not record["waiting"]:
                return False
            if not manual:
                if now < task["deadline"]:
                    return False
                pending = await c.row(
                    "SELECT update_id FROM recovery_inbox WHERE state = 'pending' AND received_at < ? LIMIT 1",
                    task["deadline"],
                )
                if pending:
                    return False
                if task["mode"] == "advanced":
                    rows = await c.rows(
                        "SELECT option, COUNT(*) AS count FROM recovery_vote WHERE uuid = ? GROUP BY option",
                        uuid,
                    )
                    counts = {row["option"]: row["count"] for row in rows}
                    yes, no = counts.get("yes", 0), counts.get("no", 0)
                else:
                    poll = task.get("poll", {})
                    if not poll.get("closed"):
                        return False
                    yes, no = poll["yes"], poll["no"]
                enough = yes + no >= task["settings"].get("mini_voters", 3)
                decision = dict(
                    action="approve" if enough and yes > no else "reject",
                    yes=yes,
                    no=no,
                    reason="insufficient"
                    if not enough
                    else ("tie" if yes == no else "votes"),
                )
            task.update(phase="resolving", decision=decision, error=None)
            await self._save(c, task, now)
            return True

    async def attention(self, uuid, error, now):
        async with self.transaction() as c:
            task = await self._get(c, uuid)
            if not task or task["phase"] == "done":
                return
            if task["phase"] != "needs_attention":
                task["resume_phase"] = task["phase"]
            task.update(phase="needs_attention", error=error)
            await self._save(c, task, now)

    async def finish_approval(self, uuid, now):
        async with self.transaction() as c:
            task = await self._get(c, uuid)
            if not task or task["phase"] != "resolving" or task.get("applied"):
                return
            d = task["decision"]
            uid = UUID(uuid) if self.postgres else uuid
            await c.execute(
                "UPDATE join_request SET waiting = FALSE, result = ?, admin = ?, yes_votes = ?, no_votes = ? WHERE uuid = ?",
                d["action"] == "approve",
                d.get("admin_id"),
                d.get("yes"),
                d.get("no"),
                uid,
            )
            task.update(
                applied=True,
                phase="cleanup",
                cleanup_at=now + (0 if d.get("admin_id") else 60),
            )
            await self._save(c, task, now)

    async def _finish_external(self, c, task, action, evidence, now):
        if task.get("applied") or task["phase"] in {"cleanup", "done"}:
            return False
        uid = UUID(task["uuid"]) if self.postgres else task["uuid"]
        if task.get("decision"):
            task["superseded_decision"] = task["decision"]
        task.update(
            decision=dict(action=action, reason="external", evidence=evidence),
            applied=True,
            phase="cleanup",
            cleanup_at=now,
            error=None,
        )
        await c.execute(
            "UPDATE join_request SET waiting = FALSE, result = ?, admin = NULL, yes_votes = NULL, no_votes = NULL WHERE uuid = ?",
            True if action == "approve" else None,
            uid,
        )
        await self._save(c, task, now)
        return True

    async def finish_external(self, uuid, action, evidence, now):
        if action not in {"approve", "closed"}:
            raise ValueError("Invalid external outcome")
        async with self.transaction() as c:
            task = await self._get(c, uuid)
            return bool(task) and await self._finish_external(
                c, task, action, evidence, now
            )

    async def member_joined(self, group_id, user_id, event_date, update_id, now):
        async with self.transaction() as c:
            rows = await c.rows(
                "SELECT uuid FROM recovery_task WHERE group_id = ? AND user_id = ? AND phase NOT IN ('cleanup', 'done')",
                group_id,
                user_id,
            )
            for row in rows:
                task = await self._get(c, row["uuid"])
                if update_id <= task.get("source_update", -1) or event_date < task.get(
                    "request_date", task.get("created_at", 0)
                ):
                    continue
                await self._finish_external(
                    c, task, "approve", f"chat_member:{update_id}", now
                )

    async def poll(self, poll_id, snapshot, update_id, now):
        async with self.transaction() as c:
            row = await c.row(
                "SELECT uuid FROM recovery_task WHERE poll_id = ?", poll_id
            )
            if not row:
                return False
            task = await self._get(c, row["uuid"])
            previous = task.get("poll", {})
            if previous.get("closed") or update_id < previous.get("update_id", -1):
                return True
            snapshot["update_id"] = update_id
            task["poll"] = snapshot
            await self._save(c, task, now)
            return True

    async def operation(self, uuid, name):
        async with self.transaction() as c:
            row = await c.row(
                "SELECT data FROM recovery_operation WHERE uuid = ? AND name = ?",
                uuid,
                name,
            )
            return json.loads(row["data"]) if row else None

    async def save_operation(self, uuid, name, operation):
        async with self.transaction() as c:
            await c.execute(
                "INSERT INTO recovery_operation (uuid, name, state, data) VALUES (?, ?, ?, ?) ON CONFLICT (uuid, name) DO UPDATE SET state = excluded.state, data = excluded.data",
                uuid,
                name,
                operation["state"],
                json.dumps(operation, ensure_ascii=False),
            )
            # Commit the returned message identity together with the operation.
            # Startup replays poll updates before it runs preparing tasks.
            result = operation.get("result")
            if (
                operation["state"] == "success"
                and isinstance(result, dict)
                and "message_id" in result
            ):
                task = await self._get(c, uuid)
                if task:
                    ref_name = (
                        "vote" if name in {"native_poll", "advanced_poll"} else name
                    )
                    task["refs"][ref_name] = result
                    if "poll_id" in result:
                        task["poll_id"] = result["poll_id"]
                        if not task.get("poll", {}).get("closed"):
                            task["poll"] = result["poll"]
                    await self._save(c, task, task.get("created_at", 0))

    async def receive(self, updates, now):
        async with self.transaction() as c:
            for update in updates:
                await c.execute(
                    "INSERT INTO recovery_inbox (update_id, body, received_at, state) VALUES (?, ?, ?, 'pending') ON CONFLICT (update_id) DO NOTHING",
                    update["update_id"],
                    json.dumps(update, ensure_ascii=False),
                    now,
                )
            if updates:
                await c.execute(
                    "INSERT INTO recovery_meta (key, value) VALUES ('offset', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    str(updates[-1]["update_id"] + 1),
                )

    async def offset(self):
        async with self.transaction() as c:
            row = await c.row("SELECT value FROM recovery_meta WHERE key = 'offset'")
            return int(row["value"]) if row else None

    async def bind_bot(self, bot_id):
        async with self.transaction() as c:
            row = await c.row("SELECT value FROM recovery_meta WHERE key = 'bot_id'")
            if row and row["value"] != str(bot_id):
                raise ValueError(
                    "Recovery database belongs to a different Telegram bot"
                )
            await c.execute(
                "INSERT INTO recovery_meta (key, value) VALUES ('bot_id', ?) ON CONFLICT (key) DO NOTHING",
                str(bot_id),
            )

    async def pending(self, now):
        async with self.transaction() as c:
            rows = await c.rows(
                "SELECT * FROM recovery_inbox WHERE state = 'pending' ORDER BY update_id LIMIT 100"
            )
            due = []
            for row in rows:
                if row["next_at"] > now:
                    break
                due.append(row)
            return due

    async def complete_update(self, update_id, now):
        async with self.transaction() as c:
            await c.execute(
                "UPDATE recovery_inbox SET state = 'done', completed_at = ? WHERE update_id = ?",
                now,
                update_id,
            )

    async def fail_update(self, row, error, now):
        attempts = row["attempts"] + 1
        async with self.transaction() as c:
            await c.execute(
                "UPDATE recovery_inbox SET state = ?, attempts = ?, next_at = ?, error = ? WHERE update_id = ?",
                "failed" if attempts >= 10 else "pending",
                attempts,
                now + min(60, 2**attempts),
                error,
                row["update_id"],
            )
        return attempts >= 10

    async def prune(self, now):
        async with self.transaction() as c:
            await c.execute(
                "DELETE FROM recovery_inbox WHERE state = 'done' AND completed_at < ?",
                now - 7 * 86400,
            )
