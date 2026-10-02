"""Select the database backend without requiring settings for unused backends."""

from app_conf import settings


def create_database(config=None):
    if config is None:
        config = settings.get("database", {})
    backend = str(config.get("backend", "postgresql")).strip().lower()
    if backend == "postgresql":
        from utils.postgres import AsyncPostgresDB

        return AsyncPostgresDB(config)
    if backend == "sqlite":
        from utils.sqlite import AsyncSQLiteDB

        return AsyncSQLiteDB(config.get("path", "data/approvebypoll.sqlite3"))
    raise ValueError(
        f"Unsupported database.backend: {backend!r}; choose 'postgresql' or 'sqlite'"
    )


BotDatabase = create_database()
