"""Asynchronous SQLite storage with the same interface as PostgreSQL."""

from pathlib import Path
from uuid import UUID

import aiosqlite
from loguru import logger

from utils.database_defaults import DEFAULT_GROUP_SETTINGS


class AsyncSQLiteDB:
    DEFAULT_GROUP_SETTINGS = DEFAULT_GROUP_SETTINGS

    def __init__(self, path="data/approvebypoll.sqlite3"):
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError("database.path must be a non-empty SQLite file path")
        self.path = str(Path(path).expanduser())
        self.conn = None

    async def connect(self):
        if self.conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Each write is one atomic statement. Autocommit prevents concurrent bot
        # tasks from sharing an implicit transaction on this single connection.
        self.conn = await aiosqlite.connect(self.path, isolation_level=None, timeout=30)
        try:
            self.conn.row_factory = aiosqlite.Row
            async with self.conn.execute("PRAGMA journal_mode=WAL"):
                pass
            await self.ensure_tables_exist()
        except BaseException:
            await self.close()
            raise
        logger.success(f"Successfully connected to SQLite database at {self.path}")

    async def close(self):
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
            logger.info("SQLite database connection closed successfully")

    async def ensure_tables_exist(self):
        async with self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS setting (
                group_id INTEGER PRIMARY KEY,
                vote_to_join BOOLEAN NOT NULL DEFAULT TRUE,
                vote_time INTEGER NOT NULL DEFAULT 600 CHECK (vote_time BETWEEN 30 AND 3600),
                pin_msg BOOLEAN NOT NULL DEFAULT FALSE,
                clean_pinned_message BOOLEAN NOT NULL DEFAULT FALSE,
                anonymous_vote BOOLEAN NOT NULL DEFAULT TRUE,
                advanced_vote BOOLEAN NOT NULL DEFAULT FALSE,
                language VARCHAR(16) NOT NULL DEFAULT 'en_US',
                mini_voters INTEGER NOT NULL DEFAULT 3 CHECK (mini_voters BETWEEN 1 AND 500)
            );
            CREATE TABLE IF NOT EXISTS join_request (
                uuid TEXT PRIMARY KEY NOT NULL,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                request_time TEXT NOT NULL,
                waiting BOOLEAN NOT NULL,
                result BOOLEAN NULL,
                admin INTEGER NULL,
                yes_votes INTEGER NULL,
                no_votes INTEGER NULL
            );
        """):
            pass

    async def get_group_settings(self, group_id: int) -> dict:
        async with self.conn.execute(
            "SELECT * FROM setting WHERE group_id = ?", (group_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            defaults = self.DEFAULT_GROUP_SETTINGS
            async with self.conn.execute(
                """
                INSERT INTO setting (
                    group_id, vote_to_join, vote_time, pin_msg,
                    clean_pinned_message, anonymous_vote, advanced_vote, language, mini_voters
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (group_id) DO NOTHING
                """,
                (group_id, *defaults.values()),
            ):
                pass
            async with self.conn.execute(
                "SELECT * FROM setting WHERE group_id = ?", (group_id,)
            ) as cursor:
                row = await cursor.fetchone()
        result = dict(row)
        for field, default in self.DEFAULT_GROUP_SETTINGS.items():
            if isinstance(default, bool):
                result[field] = bool(result[field])
        return result

    async def update_group_setting(self, group_id: int, item: str, value) -> bool:
        if item not in self.DEFAULT_GROUP_SETTINGS:
            raise ValueError(f"Unsupported setting field: {item}")
        await self.get_group_settings(group_id)
        async with self.conn.execute(
            f"UPDATE setting SET {item} = ? WHERE group_id = ?", (value, group_id)
        ) as cursor:
            return cursor.rowcount == 1

    async def create_join_request(self, uuid: str, group_id: int, user_id: int) -> None:
        async with self.conn.execute(
            """
            INSERT INTO join_request (
                uuid, group_id, user_id, request_time, waiting, result, admin
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, TRUE, NULL, NULL)
            """,
            (str(UUID(str(uuid))), group_id, user_id),
        ):
            pass

    async def update_join_request(
        self,
        uuid: str,
        result: bool,
        admin: int | None = None,
        yes_votes: int | None = None,
        no_votes: int | None = None,
    ) -> bool:
        async with self.conn.execute(
            """
            UPDATE join_request
            SET result = ?, admin = ?, waiting = FALSE,
                yes_votes = COALESCE(?, yes_votes),
                no_votes = COALESCE(?, no_votes)
            WHERE uuid = ?
            """,
            (result, admin, yes_votes, no_votes, str(UUID(str(uuid)))),
        ) as cursor:
            return cursor.rowcount == 1

    async def has_waiting_join_request(self, group_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM join_request
                WHERE group_id = ? AND user_id = ? AND waiting = TRUE
            )
            """,
            (group_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row[0])

    async def get_join_request_waiting_by_uuid(self, uuid: str) -> bool | None:
        status = await self.get_join_request_status_by_uuid(uuid)
        return None if status is None else status["waiting"]

    async def get_join_request_status_by_uuid(self, uuid: str) -> dict | None:
        async with self.conn.execute(
            """
            SELECT uuid, group_id, user_id, waiting, result
            FROM join_request WHERE uuid = ?
            """,
            (str(UUID(str(uuid))),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        status = dict(row)
        status["uuid"] = UUID(status["uuid"])
        status["waiting"] = bool(status["waiting"])
        if status["result"] is not None:
            status["result"] = bool(status["result"])
        return status
