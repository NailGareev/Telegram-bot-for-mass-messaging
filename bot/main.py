"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

from .config import BASE_DIR, Config, load_config
from .db import Database
from . import ui
from .handlers import fallback, update
from . import updater
from .service import AccessMiddleware, sync_menu_button
from .events import Hub
from .tunnel import Tunnel
from .webapi import PAGE, WebApi

log = logging.getLogger(__name__)


def verbose(config: Config) -> bool:
    return logging.getLevelName(config.log_level) <= logging.INFO


class MiniApp:
    """Веб-сервер мини-приложения и (если нет своего адреса) туннель к нему."""

    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None
        self.tunnel = Tunnel()

    async def start(
        self, bot: Bot, db: Database, config: Config, bot_username: str, hub: Hub | None = None,
    ) -> str:
        """Возвращает публичный адрес приложения или пустую строку, если оно выключено."""
        if not config.miniapp:
            return ""
        self.runner = web.AppRunner(
            WebApi(bot, db, config, bot_username, hub).build(),
            access_log=logging.getLogger("aiohttp.access") if verbose(config) else None,
        )
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


def build_dispatcher(
    db: Database, config: Config, bot_username: str, hub: Hub | None = None,
    restarter: updater.Restarter | None = None,
) -> Dispatcher:
    dp = Dispatcher(
        db=db, config=config, bot_username=bot_username, hub=hub or Hub(),
        restarter=restarter or updater.Restarter(),
    )
    # Доступ только у администраторов и только в личных сообщениях:
    # всем остальным бот ничего не отвечает.
    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    # Порядок важен: сначала обновление проекта, затем «всё остальное» (подсказка про приложение)
    dp.include_routers(update.router, fallback.router)
    return dp


async def run() -> bool:
    """Работает, пока бот не остановят. True — нужен перезапуск (после обновления)."""
    config = load_config()
    # По умолчанию в консоль попадают только предупреждения и ошибки.
    # Подробный журнал (каждое сообщение, запросы Mini App) — LOG_LEVEL=INFO в .env.
    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Если обновлённая версия не смогла запуститься несколько раз подряд, возвращаем прежнюю.
    if updater.startup_check(BASE_DIR, config.data_dir):
        return True
    db = Database(config.db_path)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Где приложение Mini App"),
        ]
    )
    hub, restarter = Hub(), updater.Restarter()
    dp = build_dispatcher(db, config, me.username, hub, restarter)
    miniapp = MiniApp()
    try:
        ui.miniapp_url = await miniapp.start(bot, db, config, me.username, hub)
        await refresh_menu_buttons(bot, db, config)
        await updater.confirm_started(bot, config.data_dir)
        log.info("Бот @%s запущен", me.username)
        polling = asyncio.create_task(dp.start_polling(bot))
        waiter = asyncio.create_task(restarter.event.wait())
        await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if restarter.requested:
            await dp.stop_polling()
        waiter.cancel()
        await polling      # заодно пробросит ошибку, если опрос Telegram упал
        return restarter.requested
    finally:
        await miniapp.stop()
        db.close()
        await bot.session.close()


def main() -> None:
    try:
        restart = asyncio.run(run())
    except KeyboardInterrupt:
        return
    if restart:
        # Служба systemd или run.py запускают бота заново, уже на новой версии.
        sys.exit(updater.RESTART_CODE)


if __name__ == "__main__":
    main()
