# -*- coding: utf-8 -*-
# @Time    : 2026/2/16 15:05
# @Author  : KimmyXYC
# @File    : postgres.py
# @Software: PyCharm
import asyncpg
from loguru import logger
from app_conf import settings


class AsyncPostgresDB:
    DEFAULT_GROUP_SETTINGS = {
        "vote_to_join": True,
        "vote_time": 600,
        "pin_msg": False,
        "clean_pinned_message": False,
        "anonymous_vote": True,
        "advanced_vote": False,
        "language": "zh-CN",
        "mini_voters": 3,
    }

    def __init__(self):
        self.host = settings.database.host
        self.port = settings.database.port
        self.dbname = settings.database.dbname
        self.user = settings.database.user
        self.password = settings.database.password
        self.conn = None

    async def connect(self):
        """
        Connect to the PostgreSQL database using asyncpg.
        This method creates a connection pool for efficient database access.
        """
        try:
            self.conn = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.dbname,
                min_size=1,
                max_size=5,
            )
            logger.success(
                f"Successfully connected to PostgreSQL database at {self.host}:{self.port}/{self.dbname}"
            )
            await self.ensure_tables_exist()
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL database: {str(e)}")
            raise

    async def close(self):
        """
        Close the connection pool to the PostgreSQL database.
        This method should be called when the application is shutting down.
        It ensures that all connections are properly closed and resources are released.
        :return: None
        """
        try:
            await self.conn.close()
            logger.info("PostgreSQL database connection closed successfully")
        except Exception as e:
            logger.error(f"Error closing PostgreSQL database connection: {str(e)}")
            raise

    async def ensure_tables_exist(self):
        """
        Check if required tables exist and create them if they don't.
        This method is called after the database connection is established.
        """
        try:
            async with self.conn.acquire() as connection:
                # Create setting table if it doesn't exist
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS setting (
                        group_id BIGINT PRIMARY KEY,
                        vote_to_join BOOLEAN NOT NULL DEFAULT TRUE,
                        vote_time INTEGER NOT NULL DEFAULT 600 CHECK (vote_time BETWEEN 30 AND 3600),
                        pin_msg BOOLEAN NOT NULL DEFAULT FALSE,
                        clean_pinned_message BOOLEAN NOT NULL DEFAULT FALSE,
                        anonymous_vote BOOLEAN NOT NULL DEFAULT TRUE,
                        advanced_vote BOOLEAN NOT NULL DEFAULT FALSE,
                        language VARCHAR(16) NOT NULL DEFAULT 'zh-CN',
                        mini_voters INTEGER NOT NULL DEFAULT 3
                    )
                """)

                # Create join_request table if it doesn't exist
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS join_request (
                        uuid UUID PRIMARY KEY,
                        group_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,
                        request_time TIMESTAMPTZ(0) NOT NULL,
                        waiting BOOLEAN NOT NULL,
                        result BOOLEAN NULL,
                        admin BIGINT NULL,
                        yes_votes INTEGER NULL,
                        no_votes INTEGER NULL
                    )
                """)

            logger.success("Database tables checked and created if needed")
        except Exception as e:
            logger.error(f"Error ensuring tables exist: {str(e)}")
            raise

    async def get_group_settings(self, group_id: int) -> dict:
        """
        Get settings for a group as a dictionary.
        If the group does not exist, create it with default settings and return defaults.
        """
        try:
            async with self.conn.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT group_id, vote_to_join, vote_time,
                           pin_msg, clean_pinned_message, anonymous_vote, advanced_vote, language, mini_voters
                    FROM setting
                    WHERE group_id = $1
                    """,
                    group_id,
                )

                if row:
                    return dict(row)

                defaults = self.DEFAULT_GROUP_SETTINGS
                await connection.execute(
                    """
                    INSERT INTO setting (
                        group_id, vote_to_join, vote_time, pin_msg,
                        clean_pinned_message, anonymous_vote, advanced_vote, language, mini_voters
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (group_id) DO NOTHING
                    """,
                    group_id,
                    defaults["vote_to_join"],
                    defaults["vote_time"],
                    defaults["pin_msg"],
                    defaults["clean_pinned_message"],
                    defaults["anonymous_vote"],
                    defaults["advanced_vote"],
                    defaults["language"],
                    defaults["mini_voters"],
                )

                inserted_or_existing = await connection.fetchrow(
                    """
                    SELECT group_id, vote_to_join, vote_time,
                           pin_msg, clean_pinned_message, anonymous_vote, advanced_vote, language, mini_voters
                    FROM setting
                    WHERE group_id = $1
                    """,
                    group_id,
                )
                return dict(inserted_or_existing)
        except Exception as e:
            logger.error(f"Error getting/creating group settings for {group_id}: {str(e)}")
            raise

    async def create_join_request(self, uuid: str, group_id: int, user_id: int) -> None:
        """
        Create a new join_request row.
        request_time uses DB current time and waiting is True.
        """
        try:
            async with self.conn.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO join_request (
                        uuid, group_id, user_id, request_time, waiting, result, admin
                    ) VALUES ($1, $2, $3, NOW(), TRUE, NULL, NULL)
                    """,
                    uuid,
                    group_id,
                    user_id,
                )
        except Exception as e:
            logger.error(f"Error creating join_request for uuid={uuid}: {str(e)}")
            raise

    async def update_join_request(
        self,
        uuid: str,
        result: bool,
        admin: int | None = None,
        yes_votes: int | None = None,
        no_votes: int | None = None,
    ) -> bool:
        """
        Update join_request by uuid and set waiting to False.
        yes_votes/no_votes are optional and only updated when provided.
        Returns True if at least one row is updated.
        """
        try:
            async with self.conn.acquire() as connection:
                if yes_votes is None and no_votes is None:
                    execute_result = await connection.execute(
                        """
                        UPDATE join_request
                        SET result = $2, admin = $3, waiting = FALSE
                        WHERE uuid = $1
                        """,
                        uuid,
                        result,
                        admin,
                    )
                else:
                    execute_result = await connection.execute(
                        """
                        UPDATE join_request
                        SET result = $2, admin = $3, waiting = FALSE,
                            yes_votes = COALESCE($4, yes_votes),
                            no_votes = COALESCE($5, no_votes)
                        WHERE uuid = $1
                        """,
                        uuid,
                        result,
                        admin,
                        yes_votes,
                        no_votes,
                    )
                return execute_result.endswith("1")
        except Exception as e:
            logger.error(f"Error updating join_request for uuid={uuid}: {str(e)}")
            raise

    async def has_waiting_join_request(self, group_id: int, user_id: int) -> bool:
        """
        Return True only if there is a row matching group_id/user_id with waiting=True.
        """
        try:
            async with self.conn.acquire() as connection:
                exists = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM join_request
                        WHERE group_id = $1 AND user_id = $2 AND waiting = TRUE
                    )
                    """,
                    group_id,
                    user_id,
                )
                return bool(exists)
        except Exception as e:
            logger.error(
                f"Error checking waiting join_request for group_id={group_id}, user_id={user_id}: {str(e)}"
            )
            raise

    async def get_join_request_waiting_by_uuid(self, uuid: str) -> bool | None:
        """
        Query join_request by uuid and return waiting status.
        Returns None if the row does not exist.
        """
        try:
            async with self.conn.acquire() as connection:
                waiting = await connection.fetchval(
                    """
                    SELECT waiting
                    FROM join_request
                    WHERE uuid = $1
                    """,
                    uuid,
                )
                return waiting
        except Exception as e:
            logger.error(f"Error querying waiting status for uuid={uuid}: {str(e)}")
            raise


BotDatabase = AsyncPostgresDB()
