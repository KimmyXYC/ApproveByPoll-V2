# -*- coding: utf-8 -*-
# @Time    : 2026/2/16 17:12
# @Author  : KimmyXYC
# @File    : event.py
# @Software: PyCharm
from telebot import formatting, types

from app.settings_menu import handle_settings_callback, open_settings
from setting.telegrambot import BotSetting
from utils.i18n import t
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
