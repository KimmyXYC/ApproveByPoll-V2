from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.settings_menu import _handle_setting_command_with_args, _parse_time_seconds
from utils.duration import format_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2592000", 2592000),
        ("30d", 2592000),
        ("1d2h30m5s", 95405),
        ("2H", 7200),
        ("10m30s", 630),
        ("30", 30),
        ("", None),
        ("1month", None),
        ("1h2d", None),
        ("-1d", None),
    ],
)
def test_duration_input(text, expected):
    assert _parse_time_seconds(text) == expected


def test_duration_display():
    assert format_duration("zh_CN", 2592000) == "30 天"
    assert format_duration("zh_CN", 95405) == "1 天 2 小时 30 分 5 秒"


def test_command_accepts_month_and_rejects_larger_duration():
    import asyncio

    async def check():
        bot = SimpleNamespace(reply_to=AsyncMock())
        with patch(
            "app.settings_menu.BotDatabase.update_group_setting", new_callable=AsyncMock
        ) as update:
            await _handle_setting_command_with_args(
                bot, SimpleNamespace(text="/setting time 30d"), "zh_CN", -1
            )
            update.assert_awaited_once_with(
                group_id=-1, item="vote_time", value=2592000
            )
            update.reset_mock()
            await _handle_setting_command_with_args(
                bot, SimpleNamespace(text="/setting time 30d1s"), "zh_CN", -1
            )
            update.assert_not_awaited()
            assert "30 天" in bot.reply_to.call_args.args[1]

    asyncio.run(check())
