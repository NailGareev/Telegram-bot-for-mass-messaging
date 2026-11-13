"""Общие операции, которыми пользуются разные обработчики."""
from __future__ import annotations

import asyncio
import io
import logging
from contextlib import suppress
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, CallbackQuery, MenuButtonCommands, MenuButtonWebApp, Message,
    WebAppInfo,
)

from . import ai, ui

from .access import PERMS, Access, parse_perms, resolve_access
from .config import DEFAULT_BOX, DEFAULT_TEMPLATE, Config
from .db import Database, Ticket
from .imaging import Box, find_qr_boxes, qr_payload, render_ticket
from .events import Hub
from .ui import can_delete, client_block, export_txt, fmt_dt, main_menu, ticket_code


log = logging.getLogger(__name__)

PREVIEW_TOKEN = "PREVIEW_PREVIEW_"


def groq_key(db: Database, config: Config) -> str:
    """Ключ Groq: заданный в боте (раздел «Настройки»), иначе из .env."""
    return db.get_setting("groq_api_key") or config.groq_api_key


def scanner_url(db: Database, config: Config) -> str:
    """Адрес страницы-сканера: заданный в боте, иначе из .env."""
    return db.get_setting("scanner_url") or config.scanner_url


async def refresh_keyboards(bot: Bot, db: Database, config: Config, text: str) -> None:
    """Присылает всем администраторам клавиатуру заново (например, когда
    сменился адрес сканера, зашитый в кнопку «Сканировать»)."""
    targets = {i: Access(i, True, frozenset(PERMS)) for i in config.main_admin_ids}
    for a in db.list_admins():
        if a["user_id"]:
            targets.setdefault(a["user_id"], Access(a["user_id"], False, parse_perms(a["perms"])))
    for user_id, access in targets.items():
        with suppress(TelegramAPIError):
            await Screen(bot, db, user_id).menu(text, access)


async def set_scanner_url(bot: Bot, db: Database, config: Config, url: str) -> None:
    if url:
        db.set_setting("scanner_url", url)
    else:
        db.delete_setting("scanner_url")
    ui.scanner_url = ui.versioned(scanner_url(db, config))
    await refresh_keyboards(bot, db, config, "Кнопка «Сканировать» обновлена.")


async def sync_menu_button(bot: Bot, user_id: int, admin: bool = True) -> None:
    """Кнопка «Mini App» слева от поля ввода — только у администраторов."""
    if admin and ui.miniapp_url:
        button = MenuButtonWebApp(text="Mini App", web_app=WebAppInfo(url=ui.miniapp_url))
    else:
        button = MenuButtonCommands()
    with suppress(TelegramAPIError):
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=button)


def render_preview(path, box: Box, bot_username: str) -> bytes:
    return render_ticket(path, box, qr_payload(bot_username, PREVIEW_TOKEN))


async def locate_box(
    config: Config, db: Database, size: tuple[int, int],
    previous: Box | None = None, hint: str | None = None,
) -> tuple[Box | None, str, str]:
    """Ищет место под QR на черновике дизайна: ИИ + поиск образца QR на картинке.
    Возвращает (квадрат или None, откуда взят, текст проблемы)."""
    path = config.pending_path
    box, source, problem = None, "", ""
    key = groq_key(db, config)
    if key:
        try:
            box = await ai.locate_qr(path, key, config.groq_model, size, previous, hint)
            source = "ИИ определил место под QR"
        except ai.AIError as e:
            log.warning("Groq: %s", e)
            problem = f"ИИ не справился: {e}"
    else:
        problem = "Ключ Groq не задан (главный админ задаёт его в «Настройках»), ИИ отключён."

    if previous is None:  # первая попытка: уточняем по образцу QR на самой картинке
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
    return box, source, problem


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


async def ticket_png(t: Ticket, db: Database, config: Config, bot_username: str) -> bytes:
    template, box = current_template(db, config)
    return await asyncio.to_thread(
        render_ticket, template, box, qr_payload(bot_username, t["token"])
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


def scan_text(status: str, t: Ticket | None, config: Config) -> str:
    """Текст результата сканирования для чата."""
    if status == "ok":
        return (
            "✅ <b>БИЛЕТ ДЕЙСТВИТЕЛЕН</b> — первое сканирование\n\n"
            + client_block(t, config)
            + f"\n\n🕒 Отмечен: {fmt_dt(t['first_scan_at'], config)}"
        )
    if status == "repeat":
        return (
            f"⚠️ <b>ПОВТОРНОЕ СКАНИРОВАНИЕ</b> — билет сканируется уже {t['scan_count']}-й раз!\n"
            f"Первое сканирование: <b>{fmt_dt(t['first_scan_at'], config)}</b>"
            f" (админ: {escape(t['first_scan_by_name'] or '—')})\n\n"
            + client_block(t, config)
        )
    if status == "not_found":
        return (
            "❌ <b>БИЛЕТ НЕ НАЙДЕН</b>\nТакого билета нет в базе (возможно, он удалён "
            "или список был сброшен)."
        )
    if status == "unreadable":
        return "Не удалось прочитать QR на фото. Попробуйте снять ближе и ровнее."
    return "❌ Это QR-код не от нашего билета."


async def report_scan(
    screen: Screen, hub: Hub, access: Access, db: Database, config: Config,
    status: str, t: Ticket | None = None, by_name: str = "",
) -> None:
    """Результат сканирования из чата. Если у админа открыт Mini App, уведомление
    показывается в нём, иначе (или если приложение не подтвердило) — в чате."""
    if t is not None:  # проверка билета: отмечаем проход
        t, first = db.register_scan(t["id"], by_name)
        status = "ok" if first else "repeat"
    text = scan_text(status, t, config)
    event = {
        "type": "scan", "status": status,
        "ticket": ticket_dict(t, access, config) if t is not None else None,
    }

    async def fallback() -> None:
        await screen.send(text)

    if not hub.publish(screen.chat_id, event, fallback):
        await screen.send(text)
