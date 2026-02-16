# -*- coding: utf-8 -*-
# @Time    : 2026/2/16 15:05
# @Author  : KimmyXYC
# @File    : postgres.py
# @Software: PyCharm
import asyncpg
from loguru import logger
from app_conf import settings


class AsyncPostgresDB:
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
                        vote_to_join BOOLEAN NOT NULL,
                        vote_to_kick BOOLEAN NOT NULL,
                        vote_time INTEGER NOT NULL CHECK (vote_time BETWEEN 30 AND 3600),
                        pin_msg BOOLEAN NOT NULL,
                        clean_pinned_message BOOLEAN NOT NULL,
                        anonymous_vote BOOLEAN NOT NULL,
                        advanced_vote BOOLEAN NOT NULL
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
                        admin BIGINT NULL
                    )
                """)

            logger.success("Database tables checked and created if needed")
        except Exception as e:
            logger.error(f"Error ensuring tables exist: {str(e)}")
            raise

BotDatabase = AsyncPostgresDB()
