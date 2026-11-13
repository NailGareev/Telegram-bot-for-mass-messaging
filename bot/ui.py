"""Кнопки, клавиатуры и форматирование текстов."""
from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from .access import Access
from .config import Config
from .db import Ticket

BTN_NEW = "🎟 Новый билет"
BTN_SCAN = "📷 Сканировать"
BTN_LIST = "📋 Список"
BTN_SEARCH = "🔎 Поиск"
BTN_STATS = "📊 Статистика"
BTN_ADMINS = "👥 Админы"
BTN_DESIGN = "🎨 Дизайн билета"
BTN_RESET = "🗑 Сброс списка"

MENU_TEXTS = {
    BTN_NEW, BTN_SCAN, BTN_LIST, BTN_SEARCH, BTN_STATS,
    BTN_ADMINS, BTN_DESIGN, BTN_RESET,
}
# Обычный текст, введённый пользователем (не кнопка меню и не команда).
PLAIN_TEXT = F.text & ~F.text.in_(MENU_TEXTS) & ~F.text.startswith("/")

# Адрес страницы-сканера (мини-приложение Telegram). Задаётся при запуске из
# настроек; если пуст — кнопка «Сканировать» работает как обычная текстовая.
scanner_url = ""


def main_menu(access: Access) -> ReplyKeyboardMarkup:
    """Главное меню: только разделы, на которые у админа есть разрешение.
    «Список» и «Поиск» есть у всех — без разрешения они показывают свои билеты."""
    buttons = []
    if access.can("new"):
        buttons.append(KeyboardButton(text=BTN_NEW))
    if access.can("scan"):
        buttons.append(
            KeyboardButton(
                text=BTN_SCAN,
                web_app=WebAppInfo(url=scanner_url) if scanner_url else None,
            )
        )
    buttons += [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_SEARCH)]
    for perm, text in (
        ("stats", BTN_STATS), ("admins", BTN_ADMINS),
        ("design", BTN_DESIGN), ("reset", BTN_RESET),
    ):
        if access.can(perm):
            buttons.append(KeyboardButton(text=text))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def inline(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


CANCEL = ("❌ Отмена", "cancel")


def cancel_kb() -> InlineKeyboardMarkup:
    return inline([CANCEL])


def ticket_code(config: Config, ticket_id: int) -> str:
    return f"{config.ticket_prefix}-{ticket_id:04d}"


def fmt_dt(iso: str | None, config: Config) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(config.tz).strftime("%d.%m.%Y %H:%M")


def client_block(t: Ticket, config: Config) -> str:
    return (
        f"🎟 Билет <b>{ticket_code(config, t['id'])}</b>\n"
        f"👤 ФИ: <b>{escape(t['full_name'])}</b>\n"
        f"👥 Группа: {escape(t['study_group'])}\n"
        f"🎓 Универ: {escape(t['university'])}\n"
        f"📞 Телефон: {escape(t['phone'])}"
    )


def ticket_card(t: Ticket, config: Config) -> str:
    text = (
        client_block(t, config)
        + f"\n🕒 Выдан: {fmt_dt(t['created_at'], config)} ({escape(t['created_by_name'])})\n"
    )
    if t["scan_count"]:
        text += (
            f"✅ Проход: {fmt_dt(t['first_scan_at'], config)}"
            f" ({escape(t['first_scan_by_name'] or '—')})"
        )
        if t["scan_count"] > 1:
            text += f"\n⚠️ Сканирований всего: {t['scan_count']}"
    else:
        text += "⏳ Ещё не сканировался"
    return text


def ticket_actions(t: Ticket, access: Access) -> InlineKeyboardMarkup:
    rows = [[("🔁 Прислать билет ещё раз", f"t:send:{t['id']}")]]
    if access.is_main and t["scan_count"]:
        rows.append([("↩️ Снять отметку о проходе", f"t:unscan:{t['id']}")])
    if can_delete(t, access):
        rows.append([("🗑 Удалить билет", f"t:del:{t['id']}")])
    return inline(*rows)


def can_delete(t: Ticket, access: Access) -> bool:
    """Удалить билет может главный админ или тот админ, который его выдал."""
    return access.is_main or t["created_by"] == access.user_id


def list_line(t: Ticket, config: Config) -> str:
    mark = "✅" if t["scan_count"] else "▫️"
    return (
        f"{mark} <b>{ticket_code(config, t['id'])}</b> {escape(t['full_name'])} · "
        f"{escape(t['study_group'])} · {escape(t['university'])} · {escape(t['phone'])}"
    )


def export_txt(tickets: list[Ticket], config: Config) -> bytes:
    scanned = sum(1 for t in tickets if t["scan_count"])
    lines = [
        "СПИСОК БИЛЕТОВ",
        f"Выгружено: {datetime.now(config.tz).strftime('%d.%m.%Y %H:%M')}",
        f"Всего билетов: {len(tickets)} | прошли: {scanned} | не прошли: {len(tickets) - scanned}",
        "=" * 60,
        "",
    ]
    for n, t in enumerate(tickets, 1):
        if t["scan_count"]:
            status = f"прошёл {fmt_dt(t['first_scan_at'], config)}"
            if t["scan_count"] > 1:
                status += f" (сканирований: {t['scan_count']})"
        else:
            status = "не сканировался"
        lines += [
            f"{n}. {ticket_code(config, t['id'])} — {t['full_name']}",
            f"   Группа: {t['study_group']}",
            f"   Универ: {t['university']}",
            f"   Телефон: {t['phone']}",
            f"   Выдан: {fmt_dt(t['created_at'], config)}, админ: {t['created_by_name']}",
            f"   Статус: {status}",
            "",
        ]
    # utf-8-sig — чтобы «Блокнот» в Windows правильно показал кириллицу
    return "\r\n".join(lines).encode("utf-8-sig")
