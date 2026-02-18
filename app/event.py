# -*- coding: utf-8 -*-
# @Time    : 2026/2/16 17:12
# @Author  : KimmyXYC
# @File    : event.py
# @Software: PyCharm
from telebot import formatting, types

from app.settings_menu import handle_settings_callback, open_settings
from setting.telegrambot import BotSetting
from utils.postgres import BotDatabase


async def set_bot_commands(bot):
    commands = [
        types.BotCommand("help", "Help"),
        types.BotCommand("setting", "Group settings"),
    ]

    await bot.set_my_commands(commands, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=types.BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=types.BotCommandScopeAllGroupChats())


async def listen_help_command(bot, message: types.Message):
    help_lines = [
        formatting.mbold("🥕 Help"),
        "",
        formatting.mbold("Commands"),
        formatting.mcite("/help - Show help information"),
        formatting.mcite("/setting - Open the group settings panel"),
        formatting.mcite("/setting time 600 - Set vote duration (30-3600 seconds)"),
        formatting.mcite(
            "/setting voter 15 or /setting mini_voters 15 - Set minimum voters (1-500)"
        ),
        "",
        formatting.mlink("🍀 Github", "https://github.com/KimmyXYC/ApproveByPoll-V2"),
    ]

    await bot.reply_to(
        message=message,
        text=formatting.format_text(*help_lines),
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


async def listen_setting_command(bot, message: types.Message):
    await open_settings(bot, message)


async def listen_setting_callback(bot, call: types.CallbackQuery):
    await handle_settings_callback(bot, call)


async def listen_pinned_service_message(bot, message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    if not message.from_user:
        return

    bot_id = int(BotSetting.bot_id) if BotSetting.bot_id else None
    if bot_id is None:
        me = await bot.get_me()
        bot_id = me.id

    if message.from_user.id != bot_id:
        return

    group_settings = await BotDatabase.get_group_settings(message.chat.id)
    if not group_settings.get("clean_pinned_message", False):
        return

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        return
