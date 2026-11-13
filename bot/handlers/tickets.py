"""Билеты: выдача, сканирование, список, поиск, статистика."""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..access import Access, Perm
from ..config import Config
from ..db import Database
from ..imaging import decode_qr, extract_token
from ..service import Screen, do_scan, download, export_file, image_file_id, ticket_file
from ..ui import (
    BTN_LIST, BTN_NEW, BTN_SCAN, BTN_SEARCH, BTN_STATS, CANCEL, PLAIN_TEXT,
    can_delete, cancel_kb, client_block, inline, list_line, ticket_actions,
    ticket_card, ticket_code,
)

router = Router()

MAX_FIELD = 100
LIST_INLINE_LIMIT = 150  # больше — показываем только файл
MAX_TICKETS_AT_ONCE = 10


class Create(StatesGroup):
    full_name = State()
    group = State()
    university = State()
    phone = State()
    confirm = State()


class Scan(StatesGroup):
    waiting = State()


class Search(StatesGroup):
    query = State()


# ---------- старт, отмена ----------


def welcome(access: Access) -> str:
    role = "главный администратор" if access.is_main else "администратор"
    text = f"Здравствуйте! Вы — <b>{role}</b>.\nДоступные разделы — на клавиатуре ниже."
    if not access.can("list"):
        text += "\n\nВ «Списке» и «Поиске» вам видны билеты, которые выдали вы."
    return text


@router.message(CommandStart(deep_link=True))
async def start_with_qr(
    message: Message, command: CommandObject, state: FSMContext,
    db: Database, config: Config, access: Access, screen: Screen,
):
    """Админ отсканировал QR камерой телефона — ссылка открыла бота."""
    token = extract_token(command.args or "")
    if token and access.can("scan"):
        return await scan_token(message, screen, token, db, config)
    await start(message, state, access, screen)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, access: Access, screen: Screen):
    await state.clear()
    await screen.clear()
    await screen.menu(welcome(access), access)


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext, screen: Screen):
    await state.clear()
    await screen.clear()


@router.callback_query(F.data == "cancel")
async def cancel_button(cb: CallbackQuery, state: FSMContext, screen: Screen):
    await state.clear()
    await screen.clear()
    await cb.answer("Отменено")


# ---------- создание билета ----------

STEPS = (
    ("full_name", "👤 ФИ"),
    ("group", "👥 Группа"),
    ("university", "🎓 Универ"),
    ("phone", "📞 Телефон"),
)


def clean(text: str) -> str:
    return " ".join(text.split())[:MAX_FIELD]


def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return None
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits


async def ask(screen: Screen, data: dict, question: str):
    """Очередной вопрос анкеты; сверху — то, что уже введено."""
    filled = "".join(
        f"{label}: {escape(data[key])}\n" for key, label in STEPS if key in data
    )
    await screen.show(
        "🎟 <b>Новый билет</b>\n\n" + filled + ("\n" if filled else "") + question,
        cancel_kb(),
    )


@router.message(F.text == BTN_NEW, Perm("new"))
async def create_start(message: Message, state: FSMContext, screen: Screen):
    await begin_create(state, screen)


async def begin_create(state: FSMContext, screen: Screen):
    await state.clear()
    await state.set_state(Create.full_name)
    await ask(screen, {}, "Введите <b>фамилию и имя</b> клиента:")


@router.message(Create.full_name, PLAIN_TEXT)
async def create_name(message: Message, state: FSMContext, screen: Screen):
    data = await state.update_data(full_name=clean(message.text))
    await state.set_state(Create.group)
    await ask(screen, data, "Введите <b>группу</b>:")


@router.message(Create.group, PLAIN_TEXT)
async def create_group(message: Message, state: FSMContext, screen: Screen):
    data = await state.update_data(group=clean(message.text))
    await state.set_state(Create.university)
    await ask(screen, data, "Введите <b>университет</b>:")


@router.message(Create.university, PLAIN_TEXT)
async def create_university(message: Message, state: FSMContext, screen: Screen):
    data = await state.update_data(university=clean(message.text))
    await state.set_state(Create.phone)
    await ask(screen, data, "Введите <b>номер телефона</b>:")


@router.message(Create.phone, PLAIN_TEXT)
async def create_phone(
    message: Message, state: FSMContext, db: Database, config: Config, screen: Screen
):
    phone = normalize_phone(message.text)
    if not phone:
        return await ask(
            screen, await state.get_data(),
            "⚠️ Не похоже на номер телефона.\n"
            "Введите <b>номер телефона</b> ещё раз, например +7 707 123 45 67:",
        )
    data = await state.update_data(phone=phone, count=1)
    await state.set_state(Create.confirm)
    await screen.clear()
    await send_confirm(state, screen, data, db, config)


async def send_confirm(
    state: FSMContext, screen: Screen, data: dict, db: Database, config: Config
):
    """Сообщение «Проверьте данные» — оно остаётся в чате и после выдачи билета."""
    text, kb = confirm_view(data, db, config)
    sent = await screen.send(text, kb)
    await state.update_data(confirm_id=sent.message_id)


def confirm_view(data: dict, db: Database, config: Config):
    count = data.get("count", 1)
    text = (
        "Проверьте данные:\n\n"
        f"👤 ФИ: <b>{escape(data['full_name'])}</b>\n"
        f"👥 Группа: {escape(data['group'])}\n"
        f"🎓 Универ: {escape(data['university'])}\n"
        f"📞 Телефон: {escape(data['phone'])}\n"
        f"🎟 Количество билетов: <b>{count}</b>"
    )
    same = db.find_by_phone(data["phone"])
    if same:
        text += "\n\n⚠️ <b>На этот номер уже выдан билет:</b>\n" + "\n".join(
            f"• {ticket_code(config, t['id'])} — {escape(t['full_name'])}" for t in same
        )
    ok = "✅ Выдать билет" if count == 1 else f"✅ Выдать билеты: {count} шт."
    kb = inline(
        [(ok, "new:ok")],
        [(f"·{n}·" if n == count else str(n), f"new:qty:{n}") for n in range(1, 6)],
        [("✏️ Ввести заново", "new:redo"), ("❌ Отмена", "new:cancel")],
    )
    return text, kb


@router.callback_query(Create.confirm, F.data.startswith("new:qty:"))
async def create_quantity(cb: CallbackQuery, state: FSMContext, db: Database, config: Config):
    count = int(cb.data.rsplit(":", 1)[1])
    data = await state.get_data()
    if data.get("count") != count:
        data = await state.update_data(count=count)
        text, kb = confirm_view(data, db, config)
        await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(f"Билетов: {count}")


@router.message(Create.confirm, PLAIN_TEXT)
async def create_quantity_typed(
    message: Message, bot: Bot, state: FSMContext, db: Database, config: Config,
    screen: Screen,
):
    """Количество билетов можно прислать числом (до 10), если кнопок 1–5 мало."""
    raw = message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_TICKETS_AT_ONCE:
        return
    data = await state.get_data()
    old_id = data.get("confirm_id")
    data = await state.update_data(count=int(raw))
    await send_confirm(state, screen, data, db, config)
    if old_id:  # прежний вариант ещё не подтверждён — заменяем его новым
        with suppress(TelegramAPIError):
            await bot.delete_message(message.chat.id, old_id)


@router.callback_query(Create.confirm, F.data == "new:ok")
async def create_confirm(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config,
    bot_username: str, screen: Screen,
):
    data = await state.get_data()
    await state.clear()
    count = max(1, min(int(data.get("count", 1)), MAX_TICKETS_AT_ONCE))
    # У каждого билета свой номер и свой уникальный QR.
    tickets = [
        db.create_ticket(
            data["full_name"], data["group"], data["university"], data["phone"],
            cb.from_user.id, cb.from_user.full_name,
        )
        for _ in range(count)
    ]
    await cb.message.edit_reply_markup()
    await cb.answer("Билет создан" if count == 1 else f"Создано билетов: {count}")
    for n, t in enumerate(tickets, 1):
        head = "✅ Билет выдан" if count == 1 else f"✅ Билет {n} из {count}"
        await screen.document(
            await ticket_file(t, db, config, bot_username),
            caption=f"{head}\n\n" + client_block(t, config),
        )


@router.callback_query(Create.confirm, F.data == "new:redo")
async def create_redo(cb: CallbackQuery, state: FSMContext, screen: Screen):
    await cb.message.edit_reply_markup()
    await cb.answer()
    await begin_create(state, screen)


@router.callback_query(F.data.startswith("new:"))
async def create_cancel(cb: CallbackQuery, state: FSMContext):
    """Отмена, а также нажатия на устаревшие кнопки подтверждения."""
    if await state.get_state() == Create.confirm.state:
        await state.clear()
    await cb.message.edit_reply_markup()
    await cb.answer("Отменено, билет не выдан", show_alert=True)


# ---------- сканирование ----------
# Итог каждого сканирования остаётся в чате — это журнал входа.


async def scan_token(message: Message, screen: Screen, token: str, db: Database, config: Config):
    t = db.get_ticket_by_token(token)
    if not t:
        return await screen.send(
            "❌ <b>БИЛЕТ НЕ НАЙДЕН</b>\nТакого билета нет в базе (возможно, он удалён "
            "или список был сброшен)."
        )
    await screen.send(do_scan(t, db, config, message.from_user.full_name))


async def scan_number(message: Message, screen: Screen, raw: str, db: Database, config: Config):
    m = re.fullmatch(r"\D*(\d{1,9})", raw.strip())
    t = db.get_ticket(int(m.group(1))) if m else None
    if not t:
        return await screen.send("❌ Билет с таким номером не найден.")
    await screen.send(do_scan(t, db, config, message.from_user.full_name))


@router.message(F.web_app_data, Perm("scan"))
async def scan_from_camera(
    message: Message, state: FSMContext, db: Database, config: Config, screen: Screen
):
    """Результат из мини-приложения со сканером: текст QR или «#номер билета»."""
    await state.clear()
    await screen.clear()
    data = message.web_app_data.data
    token = extract_token(data)
    if token:
        return await scan_token(message, screen, token, db, config)
    if not re.fullmatch(r"#\d{1,9}", data.strip()):
        return await screen.send("❌ Это QR-код не от нашего билета.")
    await scan_number(message, screen, data, db, config)


@router.message(F.text == BTN_SCAN, Perm("scan"))
async def scan_start(message: Message, state: FSMContext, screen: Screen):
    """Текстовый режим — когда страница-сканер (SCANNER_URL) не настроена."""
    await state.clear()
    await state.set_state(Scan.waiting)
    await screen.show(
        "📷 <b>Режим сканирования</b>\n\n"
        "• Наведите обычную камеру телефона на QR и откройте ссылку — "
        "данные клиента появятся здесь.\n"
        "• Или пришлите сюда фото QR-кода.\n"
        "• Или введите номер билета (например <code>12</code>).\n\n"
        "Можно сканировать билеты подряд.",
        inline([("⏹ Закончить", "cancel")]),
    )


@router.message(Scan.waiting, PLAIN_TEXT, Perm("scan"))
async def scan_by_number(message: Message, db: Database, config: Config, screen: Screen):
    token = extract_token(message.text)
    if token:
        return await scan_token(message, screen, token, db, config)
    await scan_number(message, screen, message.text, db, config)


@router.message(StateFilter(None, Scan.waiting), F.photo | F.document, Perm("scan"))
async def scan_photo(message: Message, bot: Bot, db: Database, config: Config, screen: Screen):
    file_id = image_file_id(message)
    if not file_id:
        return
    text = await asyncio.to_thread(decode_qr, await download(bot, file_id))
    if not text:
        return await screen.send(
            "Не удалось прочитать QR на фото. Попробуйте снять ближе и ровнее."
        )
    token = extract_token(text)
    if not token:
        return await screen.send("❌ Это QR-код не от нашего билета.")
    await scan_token(message, screen, token, db, config)


# ---------- список, статистика ----------


@router.message(F.text == BTN_LIST)
async def show_list(
    message: Message, state: FSMContext, db: Database, config: Config,
    access: Access, screen: Screen,
):
    await state.clear()
    owner = access.tickets_owner
    tickets = db.all_tickets(owner)
    title = "Билеты" if owner is None else "Билеты, выданные вами"
    if not tickets:
        return await screen.show(f"📋 {title}: пока пусто.")
    scanned = sum(1 for t in tickets if t["scan_count"])
    header = f"📋 <b>{title}: {len(tickets)}</b> (прошли: {scanned})\n\n"
    if len(tickets) > LIST_INLINE_LIMIT:
        await screen.show(header + "Список большой, поэтому отправляю файлом.")
        return await screen.document(export_file(db, config, owner))

    chunks, chunk = [], header
    for t in tickets:
        line = list_line(t, config) + "\n"
        if len(chunk) + len(line) > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += line
    chunks.append(chunk)
    for i, part in enumerate(chunks):
        last = i == len(chunks) - 1
        await screen.show(
            part,
            inline([("📄 Скачать TXT", "list:txt")]) if last else None,
            add=i > 0,
        )


@router.callback_query(F.data == "list:txt")
async def list_txt(cb: CallbackQuery, db: Database, config: Config, access: Access, screen: Screen):
    await cb.answer()
    await screen.document(
        export_file(db, config, access.tickets_owner), caption="Список билетов"
    )


@router.message(F.text == BTN_STATS, Perm("stats"))
async def stats(message: Message, state: FSMContext, db: Database, screen: Screen):
    await state.clear()
    tickets = db.all_tickets()
    scanned = sum(1 for t in tickets if t["scan_count"])
    repeats = sum(1 for t in tickets if t["scan_count"] > 1)
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Выдано билетов: <b>{len(tickets)}</b>\n"
        f"Прошли на вход: <b>{scanned}</b>\n"
        f"Ещё не пришли: <b>{len(tickets) - scanned}</b>\n"
        f"Билетов с повторным сканированием: <b>{repeats}</b>"
    )
    for title, key in (("По администраторам", "created_by_name"), ("По университетам", "university")):
        top = Counter(t[key] for t in tickets).most_common(10)
        if top:
            text += f"\n\n<b>{title}:</b>\n" + "\n".join(
                f"• {escape(name)} — {n}" for name, n in top
            )
    await screen.show(text)


# ---------- поиск и карточка билета ----------

SEARCH_PROMPT = "Введите фамилию, телефон, группу или номер билета:"


@router.message(F.text == BTN_SEARCH)
async def search_start(message: Message, state: FSMContext, access: Access, screen: Screen):
    await state.clear()
    await state.set_state(Search.query)
    scope = "" if access.can("list") else " (по билетам, выданным вами)"
    await screen.show(f"🔎 <b>Поиск</b>{scope}\n\n{SEARCH_PROMPT}", cancel_kb())


@router.message(Search.query, PLAIN_TEXT)
async def search_run(
    message: Message, db: Database, config: Config, access: Access, screen: Screen
):
    query = message.text.strip()
    owner = access.tickets_owner
    found = db.search(query, owner)
    m = re.fullmatch(r"(?:[^\W\d]+-?)?0*(\d{1,9})", query)
    if m:
        by_id = db.get_ticket(int(m.group(1)))
        if (
            by_id
            and owner in (None, by_id["created_by"])
            and all(t["id"] != by_id["id"] for t in found)
        ):
            found.insert(0, by_id)
    if not found:
        return await screen.show(
            f"🔎 По запросу «{escape(query[:50])}» ничего не найдено.\n\n{SEARCH_PROMPT}",
            cancel_kb(),
        )
    if len(found) == 1:
        return await screen.show(
            ticket_card(found[0], config), ticket_actions(found[0], access)
        )
    await screen.show(
        f"🔎 Найдено по запросу «{escape(query[:50])}»:",
        inline(
            *[
                [(f"{ticket_code(config, t['id'])} · {t['full_name']}"[:60], f"t:show:{t['id']}")]
                for t in found
            ],
            [CANCEL],
        ),
    )


@router.callback_query(F.data.startswith("t:"))
async def ticket_action(
    cb: CallbackQuery, db: Database, config: Config, bot_username: str,
    access: Access, screen: Screen,
):
    _, action, raw_id = cb.data.split(":")
    t = db.get_ticket(int(raw_id))
    if not t:
        return await cb.answer("Билет не найден (удалён?)", show_alert=True)
    if access.tickets_owner not in (None, t["created_by"]):
        return await cb.answer("Этот билет выдан другим администратором", show_alert=True)
    if action == "unscan" and not access.is_main:
        return await cb.answer("Доступно только главному администратору", show_alert=True)
    if action in {"del", "delok"} and not can_delete(t, access):
        return await cb.answer(
            "Удалить билет может главный администратор или админ, который его выдал",
            show_alert=True,
        )

    if action == "show":
        await screen.show(ticket_card(t, config), ticket_actions(t, access))
    elif action == "send":
        await screen.document(
            await ticket_file(t, db, config, bot_username), caption=client_block(t, config)
        )
    elif action == "unscan":
        db.reset_scan(t["id"])
        t = db.get_ticket(t["id"])
        await cb.message.edit_text(ticket_card(t, config), reply_markup=ticket_actions(t, access))
    elif action == "del":
        await cb.message.edit_reply_markup(
            reply_markup=inline(
                [("❗️ Да, удалить", f"t:delok:{t['id']}"), ("Не удалять", f"t:keep:{t['id']}")]
            )
        )
    elif action == "keep":
        await cb.message.edit_reply_markup(reply_markup=ticket_actions(t, access))
    elif action == "delok":
        db.delete_ticket(t["id"])
        await screen.show(
            f"🗑 Билет {ticket_code(config, t['id'])} ({escape(t['full_name'])}) удалён."
        )
    await cb.answer()
