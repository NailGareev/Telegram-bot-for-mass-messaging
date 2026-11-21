"""Поддержка: журнал переписки клиентов с ботом и связь админов с клиентом.

Весь обмен клиента с ботом пишется в журнал: что написал клиент (текст, файлы, нажатия кнопок),
что ответил сам бот (каталог, билеты) и что написал оператор от имени бота. Админ с разрешением
«Поддержка» видит журнал в приложении, отвечает клиенту, скачивает его файлы и выдаёт билет.
Файлы в журнале хранятся как file_id Telegram — скачать или переслать их можно, пока они доступны
боту; сами файлы на сервере не копируются.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import (
    SendAnimation, SendAudio, SendDocument, SendMessage, SendPhoto, SendSticker, SendVideo,
    SendVideoNote, SendVoice,
)
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from .access import parse_perms
from .config import Config
from .db import Database
from . import ui

log = logging.getLogger(__name__)

SENT_AS: ContextVar[str | None] = ContextVar("sent_as", default=None)   # имя оператора, пока бот пишет от его лица
SEND_METHODS = (
    SendMessage, SendPhoto, SendDocument, SendVoice, SendVideo, SendAudio, SendSticker,
    SendAnimation, SendVideoNote,
)
ACK_EVERY = timedelta(minutes=15)    # как часто отвечаем клиенту «передано оператору»


def describe(m: Message) -> dict:
    """Тип и содержимое сообщения для журнала."""
    text = m.text or m.caption or ""
    if m.document:
        d = m.document
        return dict(kind="document", text=text, file_id=d.file_id, file_name=d.file_name or "", mime=d.mime_type or "", size=d.file_size or 0, preview=text or f"📎 {d.file_name or 'файл'}")
    if m.photo:
        p = m.photo[-1]
        return dict(kind="photo", text=text, file_id=p.file_id, size=p.file_size or 0, mime="image/jpeg", preview=text or "🖼 Фото")
    if m.voice:
        return dict(kind="voice", text=text, file_id=m.voice.file_id, mime=m.voice.mime_type or "audio/ogg", size=m.voice.file_size or 0, preview="🎤 Голосовое")
    if m.video:
        v = m.video
        return dict(kind="video", text=text, file_id=v.file_id, file_name=v.file_name or "", mime=v.mime_type or "video/mp4", size=v.file_size or 0, preview=text or "🎬 Видео")
    if m.video_note:
        return dict(kind="video", text=text, file_id=m.video_note.file_id, mime="video/mp4", size=m.video_note.file_size or 0, preview="⏺ Видеосообщение")
    if m.audio:
        a = m.audio
        return dict(kind="audio", text=text, file_id=a.file_id, file_name=a.file_name or a.title or "", mime=a.mime_type or "audio/mpeg", size=a.file_size or 0, preview=text or "🎵 Аудио")
    if m.animation:
        return dict(kind="video", text=text, file_id=m.animation.file_id, mime=m.animation.mime_type or "video/mp4", preview="GIF")
    if m.sticker:
        return dict(kind="sticker", text=m.sticker.emoji or "", file_id=m.sticker.file_id, preview=f"Стикер {m.sticker.emoji or ''}".strip())
    if m.contact:
        c = m.contact
        return dict(kind="contact", text=f"{c.first_name} {c.phone_number}", preview="👤 Контакт")
    if m.location:
        return dict(kind="location", text=f"{m.location.latitude}, {m.location.longitude}", preview="📍 Геопозиция")
    return dict(kind="text" if text else "other", text=text or "", preview=text or "[сообщение]")


def button_labels(markup) -> list[str]:
    if isinstance(markup, InlineKeyboardMarkup):
        return [b.text for row in markup.inline_keyboard for b in row]
    return []


def is_staff(db: Database, config: Config, user_id: int) -> bool:
    return config.is_main(user_id) or db.find_admin(user_id, None) is not None


class ChatLogMiddleware(BaseRequestMiddleware):
    """Записывает в журнал всё, что бот отправляет клиентам."""

    def __init__(self, db: Database, config: Config):
        self.db, self.config = db, config

    async def __call__(self, make_request, bot, method):
        response = await make_request(bot, method)
        try:
            self.record(method, getattr(response, "result", response))
        except Exception:
            log.exception("Не удалось записать сообщение бота в журнал")
        return response

    def record(self, method, result) -> None:
        if not isinstance(method, SEND_METHODS) or not isinstance(result, Message):
            return
        chat = result.chat
        if chat.type != "private" or is_staff(self.db, self.config, chat.id):
            return
        info = describe(result)
        operator = SENT_AS.get()
        self.db.log_chat(
            chat.id, "admin" if operator else "bot", buttons=button_labels(getattr(method, "reply_markup", None)),
            admin_name=operator or "", **info,
        )


def log_incoming(db: Database, config: Config, event: Message | CallbackQuery) -> None:
    """Записывает в журнал то, что клиент написал или нажал (админов не пишем)."""
    user = event.from_user
    if user is None or is_staff(db, config, user.id):
        return
    db.ensure_thread(user.id, user.username, user.full_name)
    if isinstance(event, Message):
        db.log_chat(user.id, "in", **describe(event))
        return
    label = event.data or ""
    if event.message and event.message.reply_markup:
        for row in event.message.reply_markup.inline_keyboard:
            for b in row:
                if b.callback_data == event.data:
                    label = b.text
    db.log_chat(user.id, "in", kind="button", text=f"Нажал: {label}", preview=f"Нажал: {label}")


# ---------- обращения ----------


def support_admin_ids(db: Database, config: Config) -> list[int]:
    ids = set(config.main_admin_ids)
    for a in db.list_admins():
        if a["user_id"] and "support" in parse_perms(a["perms"]):
            ids.add(a["user_id"])
    return sorted(ids)


async def as_operator(name: str, make_call):
    """Выполняет отправку сообщения клиенту так, чтобы в журнале оно значилось сообщением оператора."""
    token = SENT_AS.set(name)
    try:
        return await make_call()
    finally:
        SENT_AS.reset(token)


def should_ack(thread) -> bool:
    """Отвечать ли клиенту «передано оператору»: не чаще, чем раз в 15 минут."""
    if not thread["ack_at"]:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(thread["ack_at"]) > ACK_EVERY
    except ValueError:
        return True


async def notify_admins(bot: Bot, db: Database, config: Config, hub, thread, preview: str) -> None:
    """Сообщает админам с разрешением «Поддержка» о новом сообщении клиента: в открытое приложение —
    сразу, в чат — один раз, пока обращение не откроют (чтобы не заваливать чат)."""
    who = thread["name"] or (f"@{thread['username']}" if thread["username"] else str(thread["user_id"]))
    first = not thread["notified"]
    link = ui.miniapp_url
    if link:
        link += ("&" if "?" in link else "?") + f"chat={thread['user_id']}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть чат", web_app=WebAppInfo(url=link))
    ]]) if link else None
    for admin_id in support_admin_ids(db, config):
        if hub is not None:
            hub.publish(admin_id, {"type": "chat", "user_id": thread["user_id"], "text": f"💬 {who}: {preview[:80]}"})
        if first:
            try:
                await bot.send_message(
                    admin_id, f"💬 <b>Новое сообщение в поддержке</b>\n{ui.escape(who)}: {ui.escape(preview[:200])}",
                    reply_markup=markup,
                )
            except TelegramAPIError:
                pass
    if first:
        db.update_thread(thread["user_id"], notified=1)
