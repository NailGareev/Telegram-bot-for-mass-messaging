"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import Config, load_config
from .db import Database
from . import ui
from .handlers import fallback, owner, tickets


def build_dispatcher(db: Database, config: Config, bot_username: str) -> Dispatcher:
    ui.scanner_url = config.scanner_url
    dp = Dispatcher(db=db, config=config, bot_username=bot_username)
    # Бот работает только в личных сообщениях
    dp.message.filter(F.chat.type == "private")
    # Порядок важен: сначала главный админ, затем общие функции, затем «всё остальное»
    dp.include_routers(owner.router, tickets.router, fallback.router)
    return dp


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()
    db = Database(config.db_path)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
            BotCommand(command="id", description="Мой Telegram ID"),
        ]
    )
    dp = build_dispatcher(db, config, me.username)
    logging.info("Бот @%s запущен", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
