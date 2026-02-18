# -*- coding: utf-8 -*-
# @Time    : 2026/2/16 17:12
# @Author  : KimmyXYC
# @File    : event.py
# @Software: PyCharm
from telebot import types, formatting


async def set_bot_commands(bot):
    commands = [
        types.BotCommand("help", "获取帮助信息"),
        types.BotCommand("setting", "设置"),
    ]

    await bot.set_my_commands(commands, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=types.BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=types.BotCommandScopeAllGroupChats())


async def listen_help_command(bot, message: types.Message):
    _message = await bot.reply_to(
        message=message,
        text=formatting.format_text(
            formatting.mbold("🥕 Help"),
            formatting.mlink(
                "🍀 Github", "https://github.com/KimmyXYC/ApproveByPoll-V2"
            ),
        ),
        parse_mode="MarkdownV2",
    )
