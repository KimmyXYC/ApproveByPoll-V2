# -*- coding: utf-8 -*-
# @Time    : 2026/2/16 17:12
# @Author  : KimmyXYC
# @File    : event.py
# @Software: PyCharm
from telebot import formatting, types

from app.settings_menu import handle_settings_callback, open_settings
from utils.i18n import t


async def set_bot_commands(bot):
    commands = [
        types.BotCommand("help", "Help"),
        types.BotCommand("setting", "Group settings"),
    ]

    await bot.set_my_commands(commands, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=types.BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=types.BotCommandScopeAllGroupChats())


async def listen_help_command(bot, message: types.Message):
    await bot.reply_to(
        message=message,
        text=formatting.format_text(
            formatting.mbold(f"🥕 {t('en_US', 'help_title')}"),
            formatting.mlink(
                f"🍀 {t('en_US', 'help_github')}",
                "https://github.com/KimmyXYC/ApproveByPoll-V2",
            ),
        ),
        parse_mode="MarkdownV2",
    )
