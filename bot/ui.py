"""Форматирование и мелкие помощники (билеты, даты, выгрузка)."""
from __future__ import annotations

import hashlib
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .access import Access
from .config import BASE_DIR, Config
from .db import Ticket

# Адрес мини-приложения (появляется после запуска веб-сервера и туннеля).
miniapp_url = ""


def versioned(url: str, page=BASE_DIR / "webapp" / "index.html") -> str:
    """Добавляет к адресу страницы метку её версии. Telegram кэширует
    страницы мини-приложений, и без метки после обновления страницы
    админы ещё долго видели бы старую версию."""
    if not url or not page.exists():
        return url
    version = hashlib.md5(page.read_bytes()).hexdigest()[:8]
    return f"{url}{'&' if '?' in url else '?'}v={version}"


def inline(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


def ticket_code(config: Config, ticket_id: int) -> str:
    return f"{config.ticket_prefix}-{ticket_id:04d}"


def fmt_dt(iso: str | None, config: Config) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(config.tz).strftime("%d.%m.%Y %H:%M")


def dash(value: str | None) -> str:
    """Необязательные поля (группа, университет, телефон) могут быть пустыми."""
    return escape(value) if value else "—"


def client_block(t: Ticket, config: Config) -> str:
    return (
        f"🎟 Билет <b>{ticket_code(config, t['id'])}</b>\n"
        f"👤 ФИ: <b>{escape(t['full_name'])}</b>\n"
        f"👥 Группа: {dash(t['study_group'])}\n"
        f"🎓 Универ: {dash(t['university'])}\n"
        f"📞 Телефон: {dash(t['phone'])}"
    )


def can_delete(t: Ticket, access: Access) -> bool:
    """Удалить билет может главный админ или тот админ, который его выдал."""
    return access.is_main or t["created_by"] == access.user_id


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
            f"   Группа: {t['study_group'] or '—'}",
            f"   Универ: {t['university'] or '—'}",
            f"   Телефон: {t['phone'] or '—'}",
            f"   Выдан: {fmt_dt(t['created_at'], config)}, админ: {t['created_by_name']}",
            f"   Статус: {status}",
            "",
        ]
    # utf-8-sig — чтобы «Блокнот» в Windows правильно показал кириллицу
    return "\r\n".join(lines).encode("utf-8-sig")
