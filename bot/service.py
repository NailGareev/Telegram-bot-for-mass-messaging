"""Общие операции, которыми пользуются разные обработчики."""
from __future__ import annotations

import asyncio
import io
from contextlib import suppress
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .access import Access, resolve_access
from .config import DEFAULT_BOX, DEFAULT_TEMPLATE, Config
from .db import Database, Ticket
from .imaging import Box, qr_payload, render_ticket
from .ui import client_block, export_txt, fmt_dt, main_menu, ticket_code


class Screen:
    """«Экран» раздела в чате с админом.

    Каждое новое сообщение раздела заменяет предыдущее: старое удаляется, новое
    отправляется. Результаты работы (билеты, файлы, итог сканирования, сообщение
    «Проверьте данные») отправляются через send/document и остаются в чате.
    """

    def __init__(self, bot: Bot, db: Database, chat_id: int):
        self.bot, self.db, self.chat_id = bot, db, chat_id
        self._key = f"screen:{chat_id}"
        self._anchor_key = f"menu:{chat_id}"

    async def _delete(self, ids: list[int]) -> None:
        if ids:
            with suppress(TelegramAPIError):
                await self.bot.delete_messages(self.chat_id, ids)

    async def _replace(self, message: Message, add: bool) -> Message:
        old = self.db.get_setting(self._key, [])
        if add:
            self.db.set_setting(self._key, old + [message.message_id])
        else:
            await self._delete(old)
            self.db.set_setting(self._key, [message.message_id])
        return message

    async def show(self, text: str, reply_markup=None, *, add: bool = False) -> Message:
        """Показать сообщение раздела вместо предыдущего (add=True — в дополнение)."""
        message = await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup)
        return await self._replace(message, add)

    async def photo(self, photo, caption: str, reply_markup=None) -> Message:
        message = await self.bot.send_photo(
            self.chat_id, photo, caption=caption, reply_markup=reply_markup
        )
        return await self._replace(message, add=False)

    async def clear(self) -> None:
        await self._delete(self.db.get_setting(self._key, []))
        self.db.delete_setting(self._key)

    async def send(self, text: str, reply_markup=None) -> Message:
        """Сообщение, которое остаётся в чате."""
        return await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup)

    async def document(self, file, caption: str | None = None) -> Message:
        return await self.bot.send_document(self.chat_id, file, caption=caption)

    async def menu(self, text: str, access: Access) -> None:
        """Сообщение с клавиатурой главного меню. Оно не удаляется при переходах
        (иначе Telegram спрятал бы клавиатуру) — только заменяется новым меню."""
        message = await self.bot.send_message(
            self.chat_id, text, reply_markup=main_menu(access)
        )
        old = self.db.get_setting(self._anchor_key)
        if old:
            await self._delete([old])
        self.db.set_setting(self._anchor_key, message.message_id)


class AccessMiddleware(BaseMiddleware):
    """Пускает к боту только администраторов. Всем остальным бот не отвечает
    вообще ничего. Заодно убирает из чата сообщения самого админа после
    обработки, чтобы в чате оставались только экран раздела и результаты."""

    async def __call__(self, handler, event: Message | CallbackQuery, data: dict):
        user = event.from_user
        message = event if isinstance(event, Message) else event.message
        if user is None or message is None or message.chat.type != "private":
            return None
        db, config = data["db"], data["config"]
        access = resolve_access(user, db, config)
        if access is None:
            return None
        data["access"] = access
        data["screen"] = Screen(data["bot"], db, message.chat.id)
        try:
            return await handler(event, data)
        finally:
            if isinstance(event, Message):
                with suppress(TelegramAPIError):
                    await event.delete()


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


def export_file(db: Database, config: Config, owner: int | None = None) -> BufferedInputFile:
    return BufferedInputFile(
        export_txt(db.all_tickets(owner), config), filename="tickets.txt"
    )


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
