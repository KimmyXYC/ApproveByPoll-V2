import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import asyncpg

from utils.database import create_database
from utils.postgres import AsyncPostgresDB
from utils.sqlite import AsyncSQLiteDB


class DatabaseContract:
    """Run the same behavioral checks against both real database engines."""

    async def test_group_settings_and_types(self):
        group_id = -1001234567890
        settings = await self.db.get_group_settings(group_id)
        self.assertEqual(
            settings, {"group_id": group_id, **self.db.DEFAULT_GROUP_SETTINGS}
        )
        for key, value in self.db.DEFAULT_GROUP_SETTINGS.items():
            self.assertIs(type(settings[key]), type(value))
        for key, value in {
            "vote_to_join": False,
            "vote_time": 1200,
            "pin_msg": True,
            "clean_pinned_message": True,
            "anonymous_vote": False,
            "advanced_vote": True,
            "language": "zh_CN",
            "mini_voters": 5,
        }.items():
            self.assertTrue(await self.db.update_group_setting(group_id, key, value))
            result = (await self.db.get_group_settings(group_id))[key]
            self.assertEqual(result, value)
            self.assertIs(type(result), type(value))

    async def test_settings_validation_and_recovery(self):
        with self.assertRaises(ValueError):
            await self.db.update_group_setting(-1, "language = 'x'; --", True)
        for key, value in [
            ("vote_time", 29),
            ("vote_time", 2592001),
            ("mini_voters", 0),
            ("mini_voters", 501),
        ]:
            with self.assertRaises(
                (sqlite3.IntegrityError, asyncpg.CheckViolationError)
            ):
                await self.db.update_group_setting(-1, key, value)
        # A failed statement must not poison or roll back subsequent operations.
        self.assertTrue(await self.db.update_group_setting(-1, "mini_voters", 500))
        self.assertEqual((await self.db.get_group_settings(-1))["mini_voters"], 500)
        self.assertTrue(await self.db.update_group_setting(-2, "vote_time", 30))
        self.assertEqual((await self.db.get_group_settings(-2))["vote_time"], 30)
        for value in (3601, 86400, 2592000):
            self.assertTrue(await self.db.update_group_setting(-2, "vote_time", value))
            self.assertEqual((await self.db.get_group_settings(-2))["vote_time"], value)

    async def test_legacy_vote_limit_upgrade_preserves_data_and_is_idempotent(self):
        await self.db.update_group_setting(-1, "vote_time", 3600)
        await self.db.update_group_setting(-1, "language", "zh_CN")
        request_id = str(uuid4())
        await self.db.create_join_request(request_id, -1, 77)
        if self.db.recovery.postgres:
            async with self.db.conn.acquire() as connection:
                await connection.execute(
                    "ALTER TABLE setting DROP CONSTRAINT setting_vote_time_check, ADD CONSTRAINT setting_vote_time_check CHECK (vote_time BETWEEN 30 AND 3600)"
                )
                await connection.execute(
                    "CREATE INDEX setting_language_test ON setting(language)"
                )
        else:
            async with self.db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'setting'"
            ) as cursor:
                schema = (await cursor.fetchone())["sql"].replace("2592000", "3600")
            await self.db.conn.executescript(
                "BEGIN IMMEDIATE; CREATE TABLE legacy_setting ("
                + schema.split("(", 1)[1]
                + ";"
                "INSERT INTO legacy_setting SELECT * FROM setting; DROP TABLE setting;"
                "ALTER TABLE legacy_setting RENAME TO setting;"
                "CREATE INDEX setting_language_test ON setting(language);"
                "CREATE TABLE setting_audit(value INTEGER);"
                "CREATE TRIGGER setting_audit_test AFTER UPDATE ON setting BEGIN INSERT INTO setting_audit VALUES (NEW.vote_time); END; COMMIT;"
            )
        with self.assertRaises((sqlite3.IntegrityError, asyncpg.CheckViolationError)):
            await self.db.update_group_setting(-1, "vote_time", 3601)
        await self.db.close()
        await self.db.connect()
        await self.db.ensure_tables_exist()
        saved = await self.db.get_group_settings(-1)
        self.assertEqual(saved["vote_time"], 3600)
        self.assertEqual(saved["language"], "zh_CN")
        self.assertTrue(
            (await self.db.get_join_request_status_by_uuid(request_id))["waiting"]
        )
        await self.db.update_group_setting(-1, "vote_time", 2592000)
        await self.db.close()
        await self.db.connect()
        self.assertEqual((await self.db.get_group_settings(-1))["vote_time"], 2592000)
        if self.db.recovery.postgres:
            async with self.db.conn.acquire() as connection:
                self.assertIsNotNone(
                    await connection.fetchval(
                        "SELECT to_regclass('setting_language_test')"
                    )
                )
        else:
            async with self.db.conn.execute(
                "SELECT value FROM setting_audit"
            ) as cursor:
                self.assertEqual((await cursor.fetchone())[0], 2592000)
            async with self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'setting_language_test'"
            ) as cursor:
                self.assertIsNotNone(await cursor.fetchone())

    async def test_request_lifecycle_and_missing_rows(self):
        request_id = str(uuid4())
        group_id, user_id = -1001234567890, 9876543210
        self.assertIsNone(await self.db.get_join_request_status_by_uuid(request_id))
        self.assertIsNone(await self.db.get_join_request_waiting_by_uuid(request_id))
        self.assertFalse(await self.db.update_join_request(request_id, True))
        self.assertFalse(await self.db.has_waiting_join_request(group_id, user_id))
        await self.db.create_join_request(request_id, group_id, user_id)
        self.assertIs(await self.db.get_join_request_waiting_by_uuid(request_id), True)
        self.assertTrue(await self.db.has_waiting_join_request(group_id, user_id))
        self.assertFalse(await self.db.has_waiting_join_request(group_id, user_id + 1))
        self.assertFalse(await self.db.has_waiting_join_request(group_id - 1, user_id))
        status = await self.db.get_join_request_status_by_uuid(request_id)
        self.assertEqual(status["uuid"], UUID(request_id))
        self.assertEqual(status["group_id"], group_id)
        self.assertEqual(status["user_id"], user_id)
        self.assertIsNone(status["result"])
        for approved in (True, False):
            self.assertTrue(
                await self.db.update_join_request(request_id, approved, admin=user_id)
            )
            status = await self.db.get_join_request_status_by_uuid(request_id)
            self.assertIs(status["result"], approved)
            self.assertIs(status["waiting"], False)
            self.assertIs(
                await self.db.get_join_request_waiting_by_uuid(request_id), False
            )
            self.assertFalse(await self.db.has_waiting_join_request(group_id, user_id))

    async def test_vote_counts_preserve_omitted_values(self):
        request_id = str(uuid4())
        await self.db.create_join_request(request_id, -1, 1)
        self.assertTrue(
            await self.db.update_join_request(request_id, True, yes_votes=3, no_votes=2)
        )
        self.assertTrue(
            await self.db.update_join_request(request_id, False, yes_votes=0)
        )
        self.assertTrue(
            await self.db.update_join_request(request_id, True, admin=9876543210)
        )
        row = await self.read_request(request_id)
        self.assertEqual(row["yes_votes"], 0)
        self.assertEqual(row["no_votes"], 2)
        self.assertEqual(row["admin"], 9876543210)
        self.assertIsNotNone(row["request_time"])
        self.assertTrue(
            await self.db.update_join_request(request_id, False, no_votes=0)
        )
        row = await self.read_request(request_id)
        self.assertEqual(row["no_votes"], 0)
        self.assertIsNone(row["admin"])

    async def test_duplicate_requests_do_not_overwrite(self):
        request_id = str(uuid4())
        await self.db.create_join_request(request_id, -1, 1)
        with self.assertRaises((sqlite3.IntegrityError, asyncpg.UniqueViolationError)):
            await self.db.create_join_request(request_id, -2, 2)
        status = await self.db.get_join_request_status_by_uuid(request_id)
        self.assertEqual(status["group_id"], -1)
        self.assertEqual(status["user_id"], 1)

    async def test_concurrent_requests_and_first_settings(self):
        rows = await asyncio.gather(
            *(self.db.get_group_settings(-1) for _ in range(20))
        )
        self.assertTrue(all(row == rows[0] for row in rows))
        request_ids = [str(uuid4()) for _ in range(20)]
        await asyncio.gather(
            *(
                self.db.create_join_request(uid, -1, index)
                for index, uid in enumerate(request_ids)
            )
        )
        results = await asyncio.gather(
            *(
                self.db.update_join_request(uid, index % 2 == 0)
                for index, uid in enumerate(request_ids)
            )
        )
        self.assertTrue(all(results))
        for index, uid in enumerate(request_ids):
            self.assertIs(
                (await self.db.get_join_request_status_by_uuid(uid))["result"],
                index % 2 == 0,
            )

    async def test_reconnect_and_idempotent_schema(self):
        request_id = str(uuid4())
        await self.db.update_group_setting(-1, "language", "zh_TW")
        await self.db.create_join_request(request_id, -1, 9876543210)
        await self.db.update_join_request(request_id, False, yes_votes=0, no_votes=5)
        await self.db.ensure_tables_exist()
        await self.db.close()
        await self.db.close()
        await self.db.connect()
        self.assertEqual((await self.db.get_group_settings(-1))["language"], "zh_TW")
        self.assertIs(
            (await self.db.get_join_request_status_by_uuid(request_id))["result"], False
        )
        self.assertEqual((await self.read_request(request_id))["no_votes"], 5)


class SQLiteTests(DatabaseContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = create_database(
            {
                "backend": "sqlite",
                "path": str(Path(self.directory.name) / "nested" / "bot.sqlite3"),
            }
        )
        self.addAsyncCleanup(self.db.close)
        await self.db.connect()

    async def read_request(self, request_id):
        async with self.db.conn.execute(
            "SELECT * FROM join_request WHERE uuid = ?", (request_id,)
        ) as cursor:
            return dict(await cursor.fetchone())

    async def test_two_connections_share_committed_data(self):
        other = AsyncSQLiteDB(self.db.path)
        self.addAsyncCleanup(other.close)
        await other.connect()
        await asyncio.gather(
            *(
                db.update_group_setting(-1, field, value)
                for db, field, value in [
                    (self.db, "vote_time", 900),
                    (other, "mini_voters", 20),
                ]
            )
        )
        result = await other.get_group_settings(-1)
        self.assertEqual(result["vote_time"], 900)
        self.assertEqual(result["mini_voters"], 20)

    async def test_memory_database(self):
        db = AsyncSQLiteDB(":memory:")
        self.addAsyncCleanup(db.close)
        await db.connect()
        self.assertTrue(await db.update_group_setting(-1, "pin_msg", True))
        self.assertIs((await db.get_group_settings(-1))["pin_msg"], True)


@unittest.skipUnless(
    os.environ.get("TEST_POSTGRES_DSN"),
    "Set TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)
class PostgreSQLTests(DatabaseContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Never touch existing tables: each test gets its own temporary database.
        dsn = os.environ["TEST_POSTGRES_DSN"]
        self.admin = await asyncpg.connect(dsn)
        self.addAsyncCleanup(self.admin.close)
        self.dbname = "abp_test_" + uuid4().hex
        await self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        self.addAsyncCleanup(self.admin.execute, f'DROP DATABASE "{self.dbname}"')
        parsed = urlparse(dsn)
        self.db = create_database(
            {
                "backend": "postgresql",
                "host": parsed.hostname or "127.0.0.1",
                "port": parsed.port or 5432,
                "user": unquote(parsed.username or "postgres"),
                "password": unquote(parsed.password or ""),
                "dbname": self.dbname,
            }
        )
        self.addAsyncCleanup(self.db.close)
        await self.db.connect()

    async def read_request(self, request_id):
        async with self.db.conn.acquire() as connection:
            return dict(
                await connection.fetchrow(
                    "SELECT * FROM join_request WHERE uuid = $1", request_id
                )
            )


class DatabaseSelectionTests(unittest.TestCase):
    def test_toml_selection_and_environment_override(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            config_dir = Path(root) / "conf_dir"
            config_dir.mkdir()
            config = config_dir / ".secrets.toml"
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("DYNACONF_")
            }
            env["PYTHONPATH"] = str(repository)
            script = (
                "import sys; from utils.database import BotDatabase; "
                "from utils.sqlite import AsyncSQLiteDB; "
                "assert isinstance(BotDatabase, AsyncSQLiteDB); "
                "assert BotDatabase.path == 'selected.sqlite3'; "
                "assert 'asyncpg' not in sys.modules"
            )
            config.write_text(
                '[database]\nbackend = "sqlite"\npath = "selected.sqlite3"\n'
            )
            subprocess.run(
                [sys.executable, "-c", script], cwd=root, env=env, check=True
            )
            config.write_text('[database]\nbackend = "postgresql"\n')
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=root,
                env={
                    **env,
                    "DYNACONF_DATABASE__BACKEND": "sqlite",
                    "DYNACONF_DATABASE__PATH": "selected.sqlite3",
                },
                check=True,
            )

    def test_legacy_configuration_defaults_to_postgresql(self):
        self.assertIsInstance(create_database({}), AsyncPostgresDB)

    def test_sqlite_needs_no_postgresql_credentials(self):
        db = create_database({"backend": "SQLITE"})
        self.assertIsInstance(db, AsyncSQLiteDB)
        self.assertEqual(db.path, "data/approvebypoll.sqlite3")

    def test_unknown_backend_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "database.backend"):
            create_database({"backend": "mysql"})

    def test_invalid_sqlite_path(self):
        for path in (None, "", " ", 42):
            with self.assertRaisesRegex(ValueError, "database.path"):
                create_database({"backend": "sqlite", "path": path})
