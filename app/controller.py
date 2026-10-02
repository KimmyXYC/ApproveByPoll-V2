# -*- coding: utf-8 -*-
from telebot import types
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage
from loguru import logger

from setting.telegrambot import BotSetting
from app_conf import settings
from app import event
from app.settings_menu import handle_settings_callback, open_settings
from app.recovery import PropagateHandlerErrors, RecoveryManager
from utils.database import BotDatabase
from utils.i18n import t


class BotRunner:
    def __init__(self):
        if settings.get("botapi.enable", False):
            server = settings.get("botapi.api_server")
            if server:
                from telebot import apihelper, asyncio_helper

                apihelper.API_URL = f"{server}/bot{{0}}/{{1}}"
                apihelper.FILE_URL = f"{server}/file/bot{{0}}/{{1}}"
                asyncio_helper.API_URL = apihelper.API_URL
                asyncio_helper.FILE_URL = apihelper.FILE_URL
        if not BotSetting.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self.bot = AsyncTeleBot(
            BotSetting.token,
            state_storage=StateMemoryStorage(),
            exception_handler=PropagateHandlerErrors(),
        )
        self.recovery = RecoveryManager(self.bot, BotDatabase, settings)

    def register_handlers(self):
        bot = self.bot
        manager = self.recovery

        @bot.message_handler(commands=["start", "help"], chat_types=["private"])
        async def help_command(message: types.Message):
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) == 2 and parts[1].startswith("jrres_"):
                instance = await manager.instance(parts[1][6:])
                if instance:
                    await instance.handle_realtime_result_request(message)
                else:
                    await bot.reply_to(message, "Expired")
                return
            await event.listen_help_command(bot, message)

        @bot.message_handler(commands=["setting"], chat_types=["group", "supergroup"])
        async def setting_command(message):
            await open_settings(bot, message)

        @bot.message_handler(commands=["recover"], chat_types=["group", "supergroup"])
        async def recover_command(message):
            await manager.recover_command(message)

        @bot.message_handler(
            content_types=["pinned_message"], chat_types=["group", "supergroup"]
        )
        async def pinned_message(message):
            await event.listen_pinned_service_message(bot, message)

        @bot.callback_query_handler(func=lambda call: bool(call.data))
        async def callback(call):
            if call.data.startswith("setting "):
                await handle_settings_callback(bot, call)
                return
            parts = call.data.split()
            if len(parts) not in {2, 3} or parts[0] not in {"jr", "jrv", "jrs"}:
                return await manager.answer(call, "Unsupported callback")
            instance = await manager.instance(parts[1])
            if instance:
                if parts[0] == "jr" and len(parts) == 3:
                    await instance.handle_action(call, parts[2])
                elif parts[0] == "jrv" and len(parts) == 3:
                    # Button votes must originate from the task's group.
                    if (
                        not call.message
                        or call.message.chat.id != instance.task["group_id"]
                    ):
                        return await manager.answer(
                            call, instance.text("insufficient_permissions")
                        )
                    await instance.handle_vote(call, parts[2], manager.received_at)
                elif parts[0] == "jrs" and len(parts) == 2:
                    await instance.handle_status_query(call)
                else:
                    await manager.answer(call, "Unsupported callback")
                return
            if parts[0] == "jrs":
                # Completed records created before the recovery upgrade.
                try:
                    status = await BotDatabase.get_join_request_status_by_uuid(parts[1])
                except (ValueError, TypeError):
                    status = None
                if status and status["user_id"] == call.from_user.id:
                    config = await BotDatabase.get_group_settings(status["group_id"])
                    key = (
                        "jr_status_pending_label"
                        if status["waiting"]
                        else (
                            "jr_status_approve_label"
                            if status["result"] is True
                            else (
                                "jr_status_closed_label"
                                if status["result"] is None
                                else "jr_status_reject_label"
                            )
                        )
                    )
                    return await manager.answer(
                        call,
                        t(
                            config.get("language"),
                            "jr_status_query",
                            status=t(config.get("language"), key),
                        ),
                        show_alert=True,
                    )
            await manager.answer(call, "Expired")

        @bot.chat_join_request_handler()
        async def join_request(request):
            await manager.create(request)

        @bot.poll_handler(func=lambda poll: True)
        async def poll_update(poll):
            await manager.on_poll(poll)

        @bot.chat_member_handler()
        async def member_update(update):
            await manager.on_chat_member(update)

    async def run(self):
        if BotSetting.proxy_address:
            from telebot import asyncio_helper

            asyncio_helper.proxy = BotSetting.proxy_address
        self.register_handlers()
        try:
            me = await self.bot.get_me()
            self.recovery.username = me.username or ""
            self.recovery.bot_id = str(me.id)
            await event.set_bot_commands(self.bot)
            logger.success("Bot started; restoring durable requests and updates")
            await self.recovery.run()
        finally:
            from telebot import asyncio_helper

            if asyncio_helper.session_manager.session is not None:
                await self.bot.close_session()
