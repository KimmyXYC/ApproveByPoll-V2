# -*- coding: utf-8 -*-
# @Time    : 2023/11/18 上午12:18
# @File    : controller.py
# @Software: PyCharm
from asgiref.sync import sync_to_async
from loguru import logger
from telebot import types
from telebot import util
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException
from telebot.asyncio_storage import StateMemoryStorage

from setting.telegrambot import BotSetting
from app_conf import settings
from app import event
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

        self.bot = AsyncTeleBot(BotSetting.token, state_storage=StepCache)

    async def run(self):
        logger.info("🤖 Bot Start")
        bot = self.bot

        if BotSetting.proxy_address:
            from telebot import asyncio_helper

            asyncio_helper.proxy = BotSetting.proxy_address
            logger.info("🌐 Proxy tunnels are being used!")

        await event.set_bot_commands(bot)

        @bot.message_handler(commands=["start", "help"], chat_types=["private"])
        async def listen_help_command(message: types.Message):
            await event.listen_help_command(bot, message)

        @bot.chat_join_request_handler()
        async def handle_join_request(request: types.ChatJoinRequest):
            group_settings = await BotDatabase.get_group_settings(request.chat.id)
            if group_settings.get("vote_to_join", True):
                pass

        try:
            logger.success("✨ Bot 启动成功,开始轮询...")
            await bot.polling(
                non_stop=True, allowed_updates=util.update_types, skip_pending=True
            )
        except ApiTelegramException as e:
            logger.opt(exception=e).exception("ApiTelegramException")
        except Exception as e:
            logger.exception(e)
