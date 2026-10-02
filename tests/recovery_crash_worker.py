"""Subprocess helper for actual SIGKILL durability tests (never contacts Telegram)."""

import asyncio
import json
import os
import sys
from pathlib import Path

from app.recovery import RecoveryManager
from tests.test_recovery import Clock, FakeBot, request, vote_update
from utils.database import create_database


async def main():
    db = create_database(json.loads(os.environ["RECOVERY_TEST_CONFIG"]))
    ready = Path(sys.argv[1])
    scenario = sys.argv[2]
    await db.connect()
    clock, bot = Clock(), FakeBot()
    manager = RecoveryManager(bot, db, {}, clock)
    bot.manager = manager
    manager.update_id = 1
    manager.received_at = clock()
    await db.update_group_setting(request().chat.id, "advanced_vote", True)
    await db.update_group_setting(request().chat.id, "vote_time", 30)
    await db.update_group_setting(request().chat.id, "mini_voters", 1)
    await manager.create(request())
    await manager.tick()
    task = (await db.recovery.tasks())[0]
    if scenario == "inbox":
        await db.recovery.receive([vote_update(10, task["uuid"])], 1029)
    elif scenario == "vote":
        await db.recovery.vote(task["uuid"], 77, "Alice", "yes", 1029)
    elif scenario == "transaction":
        async with db.recovery.transaction() as c:
            await c.execute(
                "INSERT INTO recovery_vote (uuid, user_id, name, option, received_at) VALUES (?, ?, ?, ?, ?)",
                task["uuid"],
                77,
                "Alice",
                "yes",
                1029,
            )
            ready.write_text(task["uuid"])
            await asyncio.Event().wait()
    ready.write_text(task["uuid"])
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
