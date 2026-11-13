"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

from .config import Config, load_config
from .db import Database
from . import ui
from .handlers import fallback, manage, tickets
from .service import AccessMiddleware, scanner_url, sync_menu_button
from .tunnel import Tunnel
from .webapi import PAGE, WebApi

log = logging.getLogger(__name__)


class MiniApp:
    """Веб-сервер мини-приложения и (если нет своего адреса) туннель к нему."""

    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None
        self.tunnel = Tunnel()

    async def start(self, bot: Bot, db: Database, config: Config, bot_username: str) -> str:
        """Возвращает публичный адрес приложения или пустую строку, если оно выключено."""
        if not config.miniapp:
            return ""
        self.runner = web.AppRunner(WebApi(bot, db, config, bot_username).build())
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", config.webapp_port).start()
        except OSError as e:
            log.warning("Mini App выключен: порт %s занят (%s)", config.webapp_port, e)
            return ""
        base = config.webapp_url
        if not base and config.tunnel:
            base = await self.tunnel.start(config.webapp_port, config.data_dir)
        if not base:
            log.warning(
                "Mini App выключен: нет публичного HTTPS-адреса. Задайте WEBAPP_URL "
                "или положите cloudflared рядом с run.py (см. README)."
            )
            return ""
        log.info("Mini App доступен по адресу %s", base)
        return ui.versioned(base + "/", PAGE)

    async def stop(self) -> None:
        await self.tunnel.stop()
        if self.runner:
            await self.runner.cleanup()


async def refresh_menu_buttons(bot: Bot, db: Database, config: Config) -> None:
    """Ставит (или убирает) кнопку Mini App всем администраторам: адрес туннеля
    меняется при каждом запуске."""
    ids = set(config.main_admin_ids) | {a["user_id"] for a in db.list_admins() if a["user_id"]}
    for user_id in ids:
        await sync_menu_button(bot, user_id)


def build_dispatcher(db: Database, config: Config, bot_username: str) -> Dispatcher:
    ui.scanner_url = ui.versioned(scanner_url(db, config))
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
    miniapp = MiniApp()
    try:
        ui.miniapp_url = await miniapp.start(bot, db, config, me.username)
        await refresh_menu_buttons(bot, db, config)
        log.info("Бот @%s запущен", me.username)
        await dp.start_polling(bot)
    finally:
        await miniapp.stop()
        db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
