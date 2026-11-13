"""Только для главного администратора: админы, дизайн билета, полный сброс."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from PIL import UnidentifiedImageError

from .. import ai
from ..config import Config
from ..db import Database
from ..imaging import (
    Box, clamp_box, find_qr_boxes, image_size, qr_payload, render_ticket, save_template,
)
from ..service import Role, current_template, download, export_file, image_file_id
from ..ui import (
    BTN_ADMINS, BTN_DESIGN, BTN_RESET, PLAIN_TEXT, cancel_kb, inline, main_menu,
    pick_user_kb,
)

log = logging.getLogger(__name__)

router = Router()
router.message.filter(Role(main_only=True))
router.callback_query.filter(Role(main_only=True))

PREVIEW_TOKEN = "PREVIEW_PREVIEW_"


class AddAdmin(StatesGroup):
    waiting = State()


class Design(StatesGroup):
    upload = State()
    review = State()
    fix = State()


# ---------- администраторы ----------


def admins_view(db: Database, config: Config):
    admins = db.list_admins()
    text = "👥 <b>Администраторы</b>\n\nГлавные (из настроек): " + ", ".join(
        f"<code>{i}</code>" for i in sorted(config.main_admin_ids)
    )
    if admins:
        text += "\n\nАдмины:\n" + "\n".join(
            f"• {escape(a['name'])} — <code>{a['user_id']}</code>" for a in admins
        )
    else:
        text += "\n\nДругих админов пока нет."
    text += (
        "\n\nАдмины могут выдавать и сканировать билеты, смотреть список. "
        "Сброс списка и смена дизайна им недоступны."
    )
    rows = [[(f"❌ Убрать: {a['name']}"[:60], f"adm:del:{a['user_id']}")] for a in admins]
    rows.append([("➕ Добавить админа", "adm:add")])
    return text, inline(*rows)


@router.message(F.text == BTN_ADMINS)
async def admins_menu(message: Message, state: FSMContext, db: Database, config: Config):
    await state.clear()
    text, kb = admins_view(db, config)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "adm:add")
async def admin_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddAdmin.waiting)
    await cb.answer()
    await cb.message.answer(
        "Кого назначить админом?\n\n"
        "• нажмите «Выбрать пользователя» и выберите человека из списка, или\n"
        "• пришлите его Telegram ID числом (он увидит свой ID, написав боту /id).",
        reply_markup=pick_user_kb(),
    )


@router.message(AddAdmin.waiting, F.users_shared | PLAIN_TEXT)
async def admin_add_finish(
    message: Message, state: FSMContext, bot: Bot, db: Database, config: Config
):
    if message.users_shared:
        shared = message.users_shared.users[0]
        user_id = shared.user_id
        name = " ".join(p for p in (shared.first_name, shared.last_name) if p)
    else:
        if not re.fullmatch(r"\d{1,15}", message.text.strip()):
            return await message.answer("Нужен числовой Telegram ID. Попробуйте ещё раз:")
        user_id, name = int(message.text.strip()), ""
    await state.clear()
    menu = main_menu(True)
    if config.is_main(user_id):
        return await message.answer("Это главный администратор.", reply_markup=menu)

    notified = True
    try:
        chat = await bot.get_chat(user_id)
        name = name or chat.full_name
        await bot.send_message(
            user_id,
            "Вас назначили администратором билетов. Нажмите /start.",
        )
    except TelegramAPIError:
        notified = False
    name = name or f"ID {user_id}"
    db.add_admin(user_id, name)
    text = f"✅ {escape(name)} (<code>{user_id}</code>) теперь админ."
    if not notified:
        text += "\nПопросите его открыть бота и нажать /start — пока бот не может ему написать."
    await message.answer(text, reply_markup=menu)


@router.callback_query(F.data.startswith("adm:del:"))
async def admin_remove(cb: CallbackQuery, db: Database, config: Config):
    db.remove_admin(int(cb.data.rsplit(":", 1)[1]))
    text, kb = admins_view(db, config)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Админ убран")


# ---------- полный сброс ----------


@router.message(F.text == BTN_RESET)
async def reset_ask(message: Message, state: FSMContext, db: Database):
    await state.clear()
    n = len(db.all_tickets())
    if not n:
        return await message.answer("Список и так пуст.")
    await message.answer(
        f"⚠️ <b>Полный сброс списка клиентов</b>\n\nБудет удалено билетов: <b>{n}</b>. "
        "Все выданные QR-коды перестанут работать, нумерация начнётся заново.\n"
        "Перед удалением я пришлю резервную копию списка в TXT.\n\nУдалить?",
        reply_markup=inline([("❗️ Да, удалить всё", "reset:yes"), ("Нет", "reset:no")]),
    )


@router.callback_query(F.data == "reset:yes")
async def reset_do(cb: CallbackQuery, db: Database, config: Config):
    await cb.message.edit_reply_markup()
    if db.all_tickets():
        # Сначала резервная копия: если она не отправится, ничего не удаляем.
        await cb.message.answer_document(
            export_file(db, config), caption="Резервная копия списка перед сбросом"
        )
    n = db.reset_tickets()
    await cb.answer()
    await cb.message.answer(f"🗑 Список сброшен. Удалено билетов: {n}.")


@router.callback_query(F.data == "reset:no")
async def reset_cancel(cb: CallbackQuery):
    await cb.message.edit_text("Сброс отменён, список не тронут.")
    await cb.answer()


# ---------- дизайн билета ----------

REVIEW_KB = inline(
    [("✅ Сохранить", "dsn:save")],
    [("🔧 Сделано неправильно", "dsn:wrong"), ("❌ Отмена", "dsn:cancel")],
)


def preview(path, box: Box, bot_username: str, name: str = "preview.png") -> BufferedInputFile:
    png = render_ticket(path, box, qr_payload(bot_username, PREVIEW_TOKEN))
    return BufferedInputFile(png, filename=name)


@router.message(F.text == BTN_DESIGN)
async def design_menu(
    message: Message, state: FSMContext, db: Database, config: Config, bot_username: str
):
    await state.clear()
    path, box = current_template(db, config)
    custom = path == config.template_path
    rows = [[("🖼 Загрузить новый дизайн", "dsn:new")]]
    if custom:
        rows.append([("↩️ Вернуть стандартный", "dsn:default")])
    await message.answer_photo(
        await asyncio.to_thread(preview, path, box, bot_username),
        caption=f"Текущий дизайн билета ({'свой' if custom else 'стандартный'}), "
        "QR показан для примера.",
        reply_markup=inline(*rows),
    )


@router.callback_query(F.data == "dsn:default")
async def design_default(cb: CallbackQuery, db: Database, config: Config):
    db.delete_setting("template_box")
    config.template_path.unlink(missing_ok=True)
    await cb.message.edit_reply_markup()
    await cb.answer()
    await cb.message.answer("Возвращён стандартный дизайн билета.")


@router.callback_query(F.data == "dsn:new")
async def design_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Design.upload)
    await cb.answer()
    await cb.message.answer(
        "Пришлите картинку нового дизайна билета.\n\n"
        "Лучше отправить её <b>как файл</b> (скрепка → «Файл»), чтобы Telegram не сжал "
        "качество. ИИ найдёт место под QR-код и покажет, как будет выглядеть билет.",
        reply_markup=cancel_kb(),
    )


@router.message(Design.upload, F.photo | F.document)
async def design_upload(
    message: Message, state: FSMContext, bot: Bot, config: Config, bot_username: str
):
    file_id = image_file_id(message)
    if not file_id:
        return await message.answer("Это не картинка. Пришлите PNG или JPG.")
    raw = await download(bot, file_id)
    try:
        await asyncio.to_thread(save_template, raw, config.pending_path)
    except (UnidentifiedImageError, OSError):
        return await message.answer("Не удалось открыть картинку. Пришлите PNG или JPG.")
    await propose(message, state, config, bot_username)


async def propose(
    message: Message,
    state: FSMContext,
    config: Config,
    bot_username: str,
    previous: Box | None = None,
    hint: str | None = None,
):
    """Определяет место под QR (ИИ + поиск образца QR на картинке) и шлёт предпросмотр."""
    path = config.pending_path
    size = await asyncio.to_thread(image_size, path)
    wait = await message.answer("🤖 ИИ ищет место для QR-кода…")
    box, source, problem = None, "", ""

    if config.groq_api_key:
        try:
            box = await ai.locate_qr(
                path, config.groq_api_key, config.groq_model, size, previous, hint
            )
            source = "ИИ определил место под QR"
        except ai.AIError as e:
            log.warning("Groq: %s", e)
            problem = f"ИИ не справился: {escape(str(e))}"
    else:
        problem = "Ключ GROQ_API_KEY не задан, ИИ отключён."

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

    with suppress(TelegramAPIError):
        await wait.delete()
    if box is None:
        await state.set_state(Design.fix)
        await state.update_data(box=previous)
        return await message.answer(
            f"{problem}\n\nУкажите место вручную — три числа через пробел: "
            f"<code>x y размер</code> (в пикселях, картинка {size[0]}×{size[1]}, "
            "x и y — левый верхний угол квадрата).",
            reply_markup=inline([("🔄 Попросить ИИ ещё раз", "dsn:retry")], [("❌ Отмена", "dsn:cancel")]),
        )
    await show_preview(message, state, config, bot_username, box, source)


async def show_preview(
    message: Message, state: FSMContext, config: Config, bot_username: str,
    box: Box, source: str,
):
    await state.set_state(Design.review)
    await state.update_data(box=list(box))
    note = ""
    if box[2] < 120:
        note = "\n⚠️ QR получается мелким — может плохо сканироваться."
    await message.answer_photo(
        await asyncio.to_thread(preview, config.pending_path, box, bot_username),
        caption=f"{source}: x={box[0]}, y={box[1]}, размер={box[2]} px.{note}\n\n"
        "Так будет выглядеть билет. Всё верно?",
        reply_markup=REVIEW_KB,
    )


@router.callback_query(Design.review, F.data == "dsn:save")
async def design_save(cb: CallbackQuery, state: FSMContext, db: Database, config: Config):
    box = (await state.get_data()).get("box")
    if not box or not config.pending_path.exists():
        await state.clear()
        return await cb.answer("Черновик дизайна не найден, загрузите заново", show_alert=True)
    shutil.move(config.pending_path, config.template_path)
    db.set_setting("template_box", box)
    await state.clear()
    await cb.message.edit_reply_markup()
    await cb.answer("Сохранено")
    await cb.message.answer(
        "✅ Новый дизайн сохранён. Все следующие билеты (и повторная отправка старых) "
        "будут в этом дизайне.",
        reply_markup=main_menu(True),
    )


@router.callback_query(Design.review, F.data == "dsn:wrong")
async def design_wrong(cb: CallbackQuery, state: FSMContext, config: Config):
    await state.set_state(Design.fix)
    w, h = image_size(config.pending_path)
    await cb.message.edit_reply_markup()
    await cb.answer()
    await cb.message.answer(
        "Что не так? Варианты:\n\n"
        "• напишите словами, что исправить (например: «QR должен быть в жёлтой части "
        "справа») — ИИ попробует ещё раз;\n"
        f"• или укажите место точно: <code>x y размер</code> в пикселях "
        f"(картинка {w}×{h}, x и y — левый верхний угол квадрата);\n"
        "• или просто попросите ИИ попробовать снова.",
        reply_markup=inline([("🔄 Попросить ИИ ещё раз", "dsn:retry")], [("❌ Отмена", "dsn:cancel")]),
    )


def _prev_box(data: dict) -> Box | None:
    return tuple(data["box"]) if data.get("box") else None


@router.callback_query(Design.fix, F.data == "dsn:retry")
async def design_retry(cb: CallbackQuery, state: FSMContext, config: Config, bot_username: str):
    await cb.message.edit_reply_markup()
    await cb.answer()
    previous = _prev_box(await state.get_data())
    # previous=(0,0,0) не бывает: если прошлого варианта нет, это снова «первая попытка»
    await propose(cb.message, state, config, bot_username, previous)


@router.message(Design.fix, PLAIN_TEXT)
async def design_fix(message: Message, state: FSMContext, config: Config, bot_username: str):
    nums = re.fullmatch(r"\s*(\d+)[\s,;]+(\d+)[\s,;]+(\d+)\s*", message.text)
    if nums:
        w, h = image_size(config.pending_path)
        box = clamp_box(tuple(int(n) for n in nums.groups()), w, h)
        return await show_preview(message, state, config, bot_username, box, "Место задано вручную")
    previous = _prev_box(await state.get_data())
    await propose(message, state, config, bot_username, previous, hint=message.text)


@router.callback_query(F.data.startswith("dsn:"))
async def design_cancel(cb: CallbackQuery, state: FSMContext, config: Config):
    """Отмена, а также нажатия на устаревшие кнопки предпросмотра."""
    current = await state.get_state()
    if current and current.startswith("Design:"):
        await state.clear()
        config.pending_path.unlink(missing_ok=True)
    await cb.message.edit_reply_markup()
    await cb.answer("Отменено")
    await cb.message.answer(
        "Смена дизайна отменена, действует прежний.", reply_markup=main_menu(True)
    )
