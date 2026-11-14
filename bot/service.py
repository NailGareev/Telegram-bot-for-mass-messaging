"""Общие операции: доступ, отрисовка билетов, поиск места под QR и ФИ."""
from __future__ import annotations

import asyncio
import io
import logging
from contextlib import suppress
from pathlib import Path

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, CallbackQuery, MenuButtonCommands, MenuButtonWebApp, Message,
    WebAppInfo,
)

from . import ai, ui

from .access import Access, resolve_access
from .config import DEFAULT_BOX, DEFAULT_NAME, DEFAULT_TEMPLATE, Config
from .db import Database, Ticket
from .imaging import SAMPLE_NAME, Box, find_qr_boxes, qr_payload, render_ticket
from .ui import can_delete, export_txt, fmt_dt, ticket_code


log = logging.getLogger(__name__)

PREVIEW_TOKEN = "PREVIEW_PREVIEW_"


def groq_key(db: Database, config: Config) -> str:
    """Ключ Groq: заданный в приложении (Настройки), иначе из .env."""
    return db.get_setting("groq_api_key") or config.groq_api_key


async def sync_menu_button(bot: Bot, user_id: int, admin: bool = True) -> None:
    """Кнопка «Mini App» слева от поля ввода — только у администраторов."""
    if admin and ui.miniapp_url:
        button = MenuButtonWebApp(text="Mini App", web_app=WebAppInfo(url=ui.miniapp_url))
    else:
        button = MenuButtonCommands()
    with suppress(TelegramAPIError):
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=button)


def render_preview(
    path, box: Box, bot_username: str, name_layout: dict | None = None
) -> bytes:
    """Предпросмотр дизайна: QR и (если включено) пример ФИ."""
    return render_ticket(
        path, box, qr_payload(bot_username, PREVIEW_TOKEN), SAMPLE_NAME, name_layout
    )


async def locate_box(
    config: Config, db: Database, size: tuple[int, int],
    previous: Box | None = None, hint: str | None = None,
    previous_name: dict | None = None,
) -> tuple[Box | None, dict | None, str, str]:
    """Ищет на черновике дизайна место под QR и место, где можно печатать ФИ:
    ИИ + поиск образца QR на самой картинке.
    Возвращает (квадрат под QR или None, раскладка ФИ или None, откуда взято, проблема)."""
    path = config.pending_path
    box, name, source, problem = None, None, "", ""
    key = groq_key(db, config)
    if key:
        try:
            box, name = await ai.locate_layout(
                path, key, config.groq_model, size, previous, previous_name, hint
            )
            source = "ИИ определил место под QR и под ФИ" if name else "ИИ определил место под QR"
        except ai.AIError as e:
            log.warning("Groq: %s", e)
            problem = f"ИИ не справился: {e}"
    else:
        problem = "Ключ Groq не задан (главный админ задаёт его в «Настройках»), ИИ отключён."

    if previous is None and previous_name is None:  # первая попытка: уточняем по образцу QR на картинке
        detected = await asyncio.to_thread(find_qr_boxes, path)
        if detected and box:
            cx, cy = box[0] + box[2] / 2, box[1] + box[2] / 2
            near = min(
                detected,
                key=lambda d: (d[0] + d[2] / 2 - cx) ** 2 + (d[1] + d[2] / 2 - cy) ** 2,
            )
            overlap = (
                min(box[0] + box[2], near[0] + near[2]) > max(box[0], near[0])
                and min(box[1] + box[2], near[1] + near[2]) > max(box[1], near[1])
            )
            if overlap:
                box, source = near, "ИИ нашёл QR, положение уточнено по образцу"
        elif detected:
            box = max(detected, key=lambda d: d[2])
            source = "На картинке найден образец QR-кода"
    return box, name, source, problem


class Screen:
    """«Экран» раздела в чате с админом.

    Каждое новое сообщение раздела заменяет предыдущее: старое удаляется, новое
    отправляется. Результаты работы (билеты, файлы, итог сканирования, сообщение
    «Проверьте данные») отправляются через send/document и остаются в чате.
    """

    def __init__(self, bot: Bot, db: Database, chat_id: int):
        self.bot, self.db, self.chat_id = bot, db, chat_id
        self._key = f"screen:{chat_id}"

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


class AccessMiddleware(BaseMiddleware):
    """Пускает к боту только администраторов. Всем остальным бот не отвечает
    вообще ничего."""

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
        return await handler(event, data)


def current_template(db: Database, config: Config) -> tuple[Path, Box, dict | None]:
    """Действующий дизайн: (файл шаблона, квадрат под QR, раскладка ФИ или None)."""
    box = db.get_setting("template_box")
    if box and config.template_path.exists():
        name = db.get_setting("template_name")
        return config.template_path, tuple(box), name if name and name.get("enabled") else None
    return DEFAULT_TEMPLATE, DEFAULT_BOX, DEFAULT_NAME


async def ticket_png(t: Ticket, db: Database, config: Config, bot_username: str) -> bytes:
    template, box, name_layout = current_template(db, config)
    return await asyncio.to_thread(
        render_ticket, template, box, qr_payload(bot_username, t["token"]),
        t["full_name"], name_layout,
    )


async def ticket_file(
    t: Ticket, db: Database, config: Config, bot_username: str
) -> BufferedInputFile:
    png = await ticket_png(t, db, config, bot_username)
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


def ticket_dict(t: Ticket, access: Access, config: Config) -> dict:
    """Билет в виде, в котором его получает Mini App."""
    return {
        "id": t["id"],
        "code": ticket_code(config, t["id"]),
        "full_name": t["full_name"],
        "group": t["study_group"],
        "university": t["university"],
        "phone": t["phone"],
        "created_at": fmt_dt(t["created_at"], config),
        "created_by": t["created_by_name"],
        "scan_count": t["scan_count"],
        "first_scan_at": fmt_dt(t["first_scan_at"], config) if t["scan_count"] else None,
        "first_scan_by": t["first_scan_by_name"],
        "can_delete": can_delete(t, access),
        "can_unscan": access.is_main and bool(t["scan_count"]),
    }


def admin_label(a) -> str:
    return a["name"] if a["user_id"] else f"@{a['username']}"


def mask(secret: str) -> str:
    return "…" + secret[-4:] if len(secret) > 8 else "…"


async def notify_admin(bot: Bot, a, text: str) -> bool:
    """Сообщает админу о назначении (если он уже открывал бота) и ставит кнопку Mini App."""
    if not a["user_id"]:
        return False
    try:
        await bot.send_message(a["user_id"], text)
    except TelegramAPIError:
        return False
    await sync_menu_button(bot, a["user_id"])
    return True
