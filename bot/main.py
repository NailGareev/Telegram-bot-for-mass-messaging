"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import hashlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import BASE_DIR, Config, load_config
from .db import Database
from . import ui
from .handlers import fallback, manage, tickets
from .service import AccessMiddleware


def versioned(url: str) -> str:
    """Добавляет к адресу сканера метку версии страницы. Telegram кэширует
    страницы мини-приложений, и без метки после обновления docs/index.html
    админы ещё долго видели бы старую версию."""
    page = BASE_DIR / "docs" / "index.html"
    if not url or not page.exists():
        return url
    version = hashlib.md5(page.read_bytes()).hexdigest()[:8]
    return f"{url}{'&' if '?' in url else '?'}v={version}"


def build_dispatcher(db: Database, config: Config, bot_username: str) -> Dispatcher:
    ui.scanner_url = versioned(config.scanner_url)
    dp = Dispatcher(db=db, config=config, bot_username=bot_username)
    # Доступ только у администраторов и только в личных сообщениях:
    # всем остальным бот ничего не отвечает.
    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    # Порядок важен: разделы управления, затем билеты, затем «всё остальное»
    dp.include_routers(manage.router, tickets.router, fallback.router)
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
