# -*- coding: utf-8 -*-
# @Time    : 2023/11/18 上午12:18
# @File    : controller.py
# @Software: PyCharm
import asyncio

from asgiref.sync import sync_to_async
from loguru import logger
from telebot import types
from telebot import util
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException
from telebot.asyncio_storage import StateMemoryStorage

from setting.telegrambot import BotSetting
from app.join_request_vote import JoinRequestVote
from app_conf import settings
from app import event
from app.utils import generate_uuid
from app.settings_menu import handle_settings_callback, open_settings
from utils.i18n import normalize_language_code, t
from utils.join_request_store import JoinRequestSessionStore
from utils.postgres import BotDatabase

StepCache = StateMemoryStorage()


@sync_to_async
def sync_to_async_func():
    pass


class BotRunner(object):
    def __init__(self):
        # 检查是否启用自定义 Bot API 服务器
        if settings.botapi.enable:
            api_server = settings.botapi.api_server
            if api_server:
                from telebot import apihelper, asyncio_helper

                # 设置自定义 Bot API URL
                apihelper.API_URL = f"{api_server}/bot{{0}}/{{1}}"
                apihelper.FILE_URL = f"{api_server}/file/bot{{0}}/{{1}}"
                asyncio_helper.API_URL = apihelper.API_URL
                asyncio_helper.FILE_URL = apihelper.FILE_URL
                logger.info(f"🌐 使用自定义 Bot API 服务器: {api_server}")
            else:
                logger.warning(
                    "⚠️ 自定义 Bot API 已启用但未配置 api_server，使用官方服务器"
                )
        else:
            logger.info("🌐 使用官方 Bot API 服务器")

        if not BotSetting.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        self.bot = AsyncTeleBot(BotSetting.token, state_storage=StepCache)
        self.join_request_store = JoinRequestSessionStore()

    def _bind_join_task_cleanup(self, uuid: str, task: asyncio.Task):
        def _on_done(done_task: asyncio.Task):
            asyncio.create_task(self.join_request_store.remove(uuid))
            if done_task.cancelled():
                return
            error = done_task.exception()
            if error:
                logger.opt(exception=error).error(
                    f"join request task failed: uuid={uuid}"
                )

        task.add_done_callback(_on_done)

    async def run(self):
        logger.info("🤖 Bot Start")
        bot = self.bot

        if BotSetting.proxy_address:
            from telebot import asyncio_helper

            asyncio_helper.proxy = BotSetting.proxy_address
            logger.info("🌐 Proxy tunnels are being used!")

        await event.set_bot_commands(bot)
        logger.info("🤖 Bot commands set")

        @bot.message_handler(commands=["start", "help"], chat_types=["private"])
        async def listen_help_command(message: types.Message):
            message_text = (message.text or "").strip()
            if message_text.startswith("/start jrres_"):
                parts = message_text.split(" ", 1)
                if len(parts) == 2 and parts[1].startswith("jrres_"):
                    request_uuid = parts[1][6:]
                    instance = await self.join_request_store.get(request_uuid)
                    if instance is None:
                        await bot.reply_to(message, "Expired")
                        return
                    await instance.handle_realtime_result_request(message)
                    return
            await event.listen_help_command(bot, message)

        @bot.message_handler(commands=["setting"], chat_types=["group", "supergroup"])
        async def listen_setting_command(message: types.Message):
            await open_settings(bot, message)

        @bot.message_handler(
            content_types=["pinned_message"], chat_types=["group", "supergroup"]
        )
        async def listen_pinned_service_message(message: types.Message):
            await event.listen_pinned_service_message(bot, message)

        @bot.callback_query_handler(func=lambda call: bool(call.data))
        async def listen_callback_query(call: types.CallbackQuery):
            if not call.data:
                return

            if call.data.startswith("setting "):
                await handle_settings_callback(bot, call)
                return

            if call.data.startswith("jr "):
                parts = call.data.split(" ")
                if len(parts) != 3:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Invalid callback",
                    )
                    return
                _, request_uuid, action = parts
                instance = await self.join_request_store.get(request_uuid)
                if instance is None:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Expired",
                    )
                    return
                await instance.handle_action(call, action)
                return

            if call.data.startswith("jrv "):
                parts = call.data.split(" ")
                if len(parts) != 3:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Invalid callback",
                    )
                    return
                _, request_uuid, option = parts
                instance = await self.join_request_store.get(request_uuid)
                if instance is None:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Expired",
                    )
                    return
                await instance.handle_vote(call, option)
                return

            if call.data.startswith("jrs "):
                parts = call.data.split(" ")
                if len(parts) != 2:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Invalid callback",
                    )
                    return
                _, request_uuid = parts
                instance = await self.join_request_store.get(request_uuid)
                if instance is not None:
                    await instance.handle_status_query(call)
                    return

                status = await BotDatabase.get_join_request_status_by_uuid(request_uuid)
                if status is None:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Expired",
                    )
                    return

                if status.get("user_id") != call.from_user.id:
                    await bot.answer_callback_query(
                        callback_query_id=call.id,
                        text="Insufficient permissions.",
                        show_alert=True,
                    )
                    return

                group_settings = await BotDatabase.get_group_settings(
                    status["group_id"]
                )
                language = normalize_language_code(group_settings.get("language"))
                if status.get("waiting"):
                    label = t(language, "jr_status_pending_label")
                elif status.get("result") is True:
                    label = t(language, "jr_status_approve_label")
                else:
                    label = t(language, "jr_status_reject_label")

                await bot.answer_callback_query(
                    callback_query_id=call.id,
                    text=t(language, "jr_status_query", status=label),
                    show_alert=True,
                )
                return

            await bot.answer_callback_query(
                callback_query_id=call.id,
                text="Unsupported callback",
            )

        @bot.chat_join_request_handler()
        async def handle_join_request(request: types.ChatJoinRequest):
            group_settings = await BotDatabase.get_group_settings(request.chat.id)
            if not group_settings.get("vote_to_join", True):
                return

            waiting = await BotDatabase.has_waiting_join_request(
                group_id=request.chat.id,
                user_id=request.from_user.id,
            )
            if waiting:
                return

            uuid = generate_uuid()
            await BotDatabase.create_join_request(
                uuid=uuid,
                group_id=request.chat.id,
                user_id=request.from_user.id,
            )

            join_request_vote = JoinRequestVote(
                bot=bot,
                request=request,
                uuid=uuid,
                group_settings=group_settings,
            )
            task = asyncio.create_task(join_request_vote.run())
            await self.join_request_store.set(uuid, join_request_vote, task)
            self._bind_join_task_cleanup(uuid, task)

        try:
            logger.success("✨ Bot 启动成功,开始轮询...")
            await bot.polling(
                non_stop=True, allowed_updates=util.update_types, skip_pending=True
            )
        except ApiTelegramException as e:
            logger.opt(exception=e).exception("ApiTelegramException")
        except Exception as e:
            logger.exception(e)
