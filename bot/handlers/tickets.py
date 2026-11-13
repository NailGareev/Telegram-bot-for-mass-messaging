"""Обработчики для всех администраторов: билеты, сканирование, список, поиск."""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..db import Database
from ..imaging import decode_qr, extract_token
from ..service import Role, do_scan, download, export_file, image_file_id, ticket_file
from ..ui import (
    BTN_CANCEL, BTN_LIST, BTN_NEW, BTN_SCAN, BTN_SEARCH, BTN_STATS, PLAIN_TEXT,
    cancel_kb, client_block, inline, list_line, main_menu, ticket_actions,
    ticket_card, ticket_code,
)

router = Router()
router.message.filter(Role())
router.callback_query.filter(Role())

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


def menu_for(message_or_cb, config: Config):
    return main_menu(config.is_main(message_or_cb.from_user.id))


# ---------- старт, отмена ----------


@router.message(CommandStart(deep_link=True))
async def start_with_qr(
    message: Message, command: CommandObject, state: FSMContext,
    db: Database, config: Config,
):
    """Админ отсканировал QR камерой телефона — ссылка открыла бота."""
    token = extract_token(command.args or "")
    if not token:
        return await start(message, state, config)
    await scan_token(message, token, db, config)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, config: Config):
    await state.clear()
    role = "главный администратор" if config.is_main(message.from_user.id) else "администратор"
    await message.answer(
        f"Здравствуйте! Вы — <b>{role}</b>.\n\n"
        "🎟 <b>Новый билет</b> — внести клиента и получить билет с QR\n"
        "📷 <b>Сканировать</b> — проверка билетов на входе\n"
        "📋 <b>Список</b> — все, кто купил билет (+ выгрузка в TXT)\n"
        "🔎 <b>Поиск</b> — найти клиента по имени или телефону\n\n"
        "QR можно сканировать и обычной камерой телефона — ссылка откроет этого бота.",
        reply_markup=menu_for(message, config),
    )


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cancel(message: Message, state: FSMContext, config: Config):
    await state.clear()
    await message.answer("Отменено.", reply_markup=menu_for(message, config))


# ---------- создание билета ----------


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


@router.message(F.text == BTN_NEW)
async def create_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Create.full_name)
    await message.answer(
        "Новый билет.\n\nВведите <b>фамилию и имя</b> клиента:", reply_markup=cancel_kb()
    )


@router.message(Create.full_name, PLAIN_TEXT)
async def create_name(message: Message, state: FSMContext):
    await state.update_data(full_name=clean(message.text))
    await state.set_state(Create.group)
    await message.answer("Введите <b>группу</b>:")


@router.message(Create.group, PLAIN_TEXT)
async def create_group(message: Message, state: FSMContext):
    await state.update_data(group=clean(message.text))
    await state.set_state(Create.university)
    await message.answer("Введите <b>университет</b>:")


@router.message(Create.university, PLAIN_TEXT)
async def create_university(message: Message, state: FSMContext):
    await state.update_data(university=clean(message.text))
    await state.set_state(Create.phone)
    await message.answer("Введите <b>номер телефона</b>:")


@router.message(Create.phone, PLAIN_TEXT)
async def create_phone(message: Message, state: FSMContext, db: Database, config: Config):
    phone = normalize_phone(message.text)
    if not phone:
        return await message.answer(
            "Не похоже на номер телефона. Введите ещё раз, например +7 707 123 45 67:"
        )
    data = await state.update_data(phone=phone, count=1)
    await state.set_state(Create.confirm)
    text, kb = confirm_view(data, db, config)
    await message.answer(text, reply_markup=kb)


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
    text += (
        "\n\nНесколько билетов на эти данные — выберите количество кнопкой "
        f"или отправьте число (до {MAX_TICKETS_AT_ONCE})."
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
    message: Message, state: FSMContext, db: Database, config: Config
):
    raw = message.text.strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_TICKETS_AT_ONCE:
        return await message.answer(
            f"Отправьте количество билетов числом от 1 до {MAX_TICKETS_AT_ONCE} "
            "или нажмите кнопку под сообщением с данными."
        )
    data = await state.update_data(count=int(raw))
    text, kb = confirm_view(data, db, config)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Create.confirm, F.data == "new:ok")
async def create_confirm(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config, bot_username: str
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
        await cb.message.answer_document(
            await ticket_file(t, db, config, bot_username),
            caption=f"{head}\n\n" + client_block(t, config),
            reply_markup=menu_for(cb, config),
        )


@router.callback_query(Create.confirm, F.data == "new:redo")
async def create_redo(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup()
    await cb.answer()
    await create_start(cb.message, state)


@router.callback_query(F.data.startswith("new:"))
async def create_cancel(cb: CallbackQuery, state: FSMContext, config: Config):
    """Отмена, а также нажатия на устаревшие кнопки подтверждения."""
    if await state.get_state() == Create.confirm.state:
        await state.clear()
    await cb.message.edit_reply_markup()
    await cb.answer("Отменено")
    await cb.message.answer("Создание билета отменено.", reply_markup=menu_for(cb, config))


# ---------- сканирование ----------


async def scan_token(message: Message, token: str, db: Database, config: Config):
    t = db.get_ticket_by_token(token)
    if not t:
        return await message.answer(
            "❌ <b>БИЛЕТ НЕ НАЙДЕН</b>\nТакого билета нет в базе (возможно, он удалён "
            "или список был сброшен)."
        )
    await message.answer(do_scan(t, db, config, message.from_user.full_name))


@router.message(F.text == BTN_SCAN)
async def scan_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Scan.waiting)
    await message.answer(
        "📷 <b>Режим сканирования</b>\n\n"
        "• Наведите обычную камеру телефона на QR и откройте ссылку — "
        "данные клиента появятся здесь.\n"
        "• Или пришлите сюда фото QR-кода.\n"
        "• Или введите номер билета (например <code>12</code>).\n\n"
        "Можно сканировать билеты подряд. Для выхода нажмите «Отмена».",
        reply_markup=cancel_kb(),
    )


@router.message(Scan.waiting, PLAIN_TEXT)
async def scan_by_number(message: Message, db: Database, config: Config):
    token = extract_token(message.text)
    if token:
        return await scan_token(message, token, db, config)
    m = re.fullmatch(r"\D*(\d{1,9})", message.text.strip())
    t = db.get_ticket(int(m.group(1))) if m else None
    if not t:
        return await message.answer("❌ Билет с таким номером не найден.")
    await message.answer(do_scan(t, db, config, message.from_user.full_name))


@router.message(StateFilter(None, Scan.waiting), F.photo | F.document)
async def scan_photo(message: Message, bot: Bot, db: Database, config: Config):
    file_id = image_file_id(message)
    if not file_id:
        return await message.answer("Пришлите фото QR-кода.")
    text = await asyncio.to_thread(decode_qr, await download(bot, file_id))
    if not text:
        return await message.answer(
            "Не удалось прочитать QR на фото. Попробуйте снять ближе и ровнее "
            "или введите номер билета в режиме «Сканировать»."
        )
    token = extract_token(text)
    if not token:
        return await message.answer("❌ Это QR-код не от нашего билета.")
    await scan_token(message, token, db, config)


# ---------- список, статистика ----------


@router.message(F.text == BTN_LIST)
async def show_list(message: Message, state: FSMContext, db: Database, config: Config):
    await state.clear()
    tickets = db.all_tickets()
    if not tickets:
        return await message.answer(
            "Список пуст — билеты ещё не выдавались.", reply_markup=menu_for(message, config)
        )
    scanned = sum(1 for t in tickets if t["scan_count"])
    header = f"📋 <b>Билетов: {len(tickets)}</b> (прошли: {scanned})\n\n"
    kb = inline([("📄 Скачать TXT", "list:txt")])
    if len(tickets) > LIST_INLINE_LIMIT:
        await message.answer(
            header + "Список большой, поэтому отправляю файлом.",
            reply_markup=menu_for(message, config),
        )
        return await message.answer_document(export_file(db, config))

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
        await message.answer(part, reply_markup=kb if last else None)


@router.callback_query(F.data == "list:txt")
async def list_txt(cb: CallbackQuery, db: Database, config: Config):
    await cb.answer()
    await cb.message.answer_document(export_file(db, config), caption="Список билетов")


@router.message(F.text == BTN_STATS)
async def stats(message: Message, state: FSMContext, db: Database, config: Config):
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
    await message.answer(text, reply_markup=menu_for(message, config))


# ---------- поиск и карточка билета ----------


@router.message(F.text == BTN_SEARCH)
async def search_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Search.query)
    await message.answer(
        "Введите фамилию, телефон, группу или номер билета:", reply_markup=cancel_kb()
    )


@router.message(Search.query, PLAIN_TEXT)
async def search_run(message: Message, db: Database, config: Config):
    query = message.text.strip()
    found = db.search(query)
    m = re.fullmatch(r"(?:[^\W\d]+-?)?0*(\d{1,9})", query)
    if m:
        by_id = db.get_ticket(int(m.group(1)))
        if by_id and all(t["id"] != by_id["id"] for t in found):
            found.insert(0, by_id)
    if not found:
        return await message.answer("Ничего не найдено. Попробуйте другой запрос:")
    if len(found) == 1:
        return await send_card(message, found[0], config, message.from_user.id)
    await message.answer(
        "Найдено:",
        reply_markup=inline(
            *[
                [(f"{ticket_code(config, t['id'])} · {t['full_name']}"[:60], f"t:show:{t['id']}")]
                for t in found
            ]
        ),
    )


def can_delete(t, config: Config, user_id: int) -> bool:
    """Удалить билет может главный админ или тот админ, который его выдал."""
    return config.is_main(user_id) or t["created_by"] == user_id


def actions_for(t, config: Config, user_id: int):
    return ticket_actions(
        t["id"], config.is_main(user_id), bool(t["scan_count"]),
        can_delete(t, config, user_id),
    )


async def send_card(message: Message, t, config: Config, user_id: int):
    await message.answer(ticket_card(t, config), reply_markup=actions_for(t, config, user_id))


@router.callback_query(F.data.startswith("t:"))
async def ticket_action(cb: CallbackQuery, db: Database, config: Config, bot_username: str):
    _, action, raw_id = cb.data.split(":")
    is_main = config.is_main(cb.from_user.id)
    t = db.get_ticket(int(raw_id))
    if not t:
        return await cb.answer("Билет не найден (удалён?)", show_alert=True)
    if action == "unscan" and not is_main:
        return await cb.answer("Доступно только главному администратору", show_alert=True)
    if action in {"del", "delok"} and not can_delete(t, config, cb.from_user.id):
        return await cb.answer(
            "Удалить билет может главный администратор или админ, который его выдал",
            show_alert=True,
        )

    if action == "show":
        await send_card(cb.message, t, config, cb.from_user.id)
    elif action == "send":
        await cb.message.answer_document(
            await ticket_file(t, db, config, bot_username), caption=client_block(t, config)
        )
    elif action == "unscan":
        db.reset_scan(t["id"])
        t = db.get_ticket(t["id"])
        await cb.message.edit_text(
            ticket_card(t, config), reply_markup=actions_for(t, config, cb.from_user.id)
        )
    elif action == "del":
        await cb.message.edit_reply_markup(
            reply_markup=inline(
                [("❗️ Да, удалить", f"t:delok:{t['id']}"), ("Не удалять", f"t:keep:{t['id']}")]
            )
        )
    elif action == "keep":
        await cb.message.edit_reply_markup(
            reply_markup=actions_for(t, config, cb.from_user.id)
        )
    elif action == "delok":
        db.delete_ticket(t["id"])
        await cb.message.edit_text(
            f"🗑 Билет {ticket_code(config, t['id'])} ({escape(t['full_name'])}) удалён."
        )
    await cb.answer()
