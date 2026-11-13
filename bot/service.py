"""Общие операции, которыми пользуются разные обработчики."""
from __future__ import annotations

import asyncio
import io
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .config import DEFAULT_BOX, DEFAULT_TEMPLATE, Config
from .db import Database, Ticket
from .imaging import Box, qr_payload, render_ticket
from .ui import client_block, export_txt, fmt_dt, ticket_code


class Role(BaseFilter):
    """Пропускает только администраторов (или только главных, если main_only)."""

    def __init__(self, main_only: bool = False):
        self.main_only = main_only

    async def __call__(
        self, event: Message | CallbackQuery, db: Database, config: Config
    ) -> bool:
        user = event.from_user
        if user is None:
            return False
        if config.is_main(user.id):
            return True
        return not self.main_only and db.is_admin(user.id)


def current_template(db: Database, config: Config) -> tuple[Path, Box]:
    box = db.get_setting("template_box")
    if box and config.template_path.exists():
        return config.template_path, tuple(box)
    return DEFAULT_TEMPLATE, DEFAULT_BOX


async def ticket_file(
    t: Ticket, db: Database, config: Config, bot_username: str
) -> BufferedInputFile:
    template, box = current_template(db, config)
    png = await asyncio.to_thread(
        render_ticket, template, box, qr_payload(bot_username, t["token"])
    )
    return BufferedInputFile(png, filename=f"ticket_{ticket_code(config, t['id'])}.png")


def export_file(db: Database, config: Config) -> BufferedInputFile:
    return BufferedInputFile(export_txt(db.all_tickets(), config), filename="tickets.txt")


async def download(bot: Bot, file_id: str) -> bytes:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


def image_file_id(message: Message) -> str | None:
    """file_id картинки из сообщения: фото или документ-изображение."""
    if message.photo:
        return message.photo[-1].file_id
    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id
    return None


def do_scan(t: Ticket, db: Database, config: Config, by_name: str) -> str:
    """Отмечает сканирование билета и возвращает текст результата."""
    t, first = db.register_scan(t["id"], by_name)
    if first:
        return (
            "✅ <b>БИЛЕТ ДЕЙСТВИТЕЛЕН</b> — первое сканирование\n\n"
            + client_block(t, config)
            + f"\n\n🕒 Отмечен: {fmt_dt(t['first_scan_at'], config)}"
        )
    return (
        f"⚠️ <b>ПОВТОРНОЕ СКАНИРОВАНИЕ</b> — билет сканируется уже {t['scan_count']}-й раз!\n"
        f"Первое сканирование: <b>{fmt_dt(t['first_scan_at'], config)}</b>"
        f" (админ: {escape(t['first_scan_by_name'] or '—')})\n\n"
        + client_block(t, config)
    )
