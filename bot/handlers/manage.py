"""Управление: админы и их разрешения, дизайн билета, полный сброс списка."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from PIL import UnidentifiedImageError

from ..access import DEFAULT_PERMS, PERMS, Access, Perm, dump_perms, parse_perms
from ..config import Config
from ..db import Database
from ..imaging import Box, clamp_box, image_size, save_template
from ..service import (
    Screen, current_template, download, export_file, image_file_id, locate_box,
    render_preview, sync_menu_button,
)
from ..ui import BTN_ADMINS, BTN_DESIGN, BTN_RESET, CANCEL, PLAIN_TEXT, cancel_kb, inline

log = logging.getLogger(__name__)

router = Router()

class AddAdmin(StatesGroup):
    waiting = State()


class Design(StatesGroup):
    upload = State()
    review = State()
    fix = State()


# ---------- администраторы ----------

ADD_PROMPT = (
    "➕ <b>Новый админ</b>\n\n"
    "Пришлите его ник в Telegram (например <code>@username</code>) "
    "или числовой Telegram ID."
)


def admin_label(a) -> str:
    return a["name"] if a["user_id"] else f"@{a['username']}"


def admins_view(db: Database, config: Config):
    admins = db.list_admins()
    text = "👥 <b>Администраторы</b>\n\nГлавные (из настроек): " + ", ".join(
        f"<code>{i}</code>" for i in sorted(config.main_admin_ids)
    )
    if admins:
        text += "\n\nНажмите на админа, чтобы настроить его разрешения."
    else:
        text += "\n\nДругих админов пока нет."
    rows = [[(f"⚙️ {admin_label(a)}"[:60], f"adm:edit:{a['id']}")] for a in admins]
    rows.append([("➕ Добавить админа", "adm:add")])
    return text, inline(*rows)


def admin_view(a, access: Access):
    perms = parse_perms(a["perms"])
    text = f"⚙️ <b>{escape(admin_label(a))}</b>\n"
    if a["username"]:
        text += f"Ник: @{escape(a['username'])}\n"
    if a["user_id"]:
        text += f"ID: <code>{a['user_id']}</code>\n"
    else:
        text += "Ещё не заходил в бота — доступ включится, когда он напишет боту /start.\n"
    text += "\n<b>Разрешения</b> (нажмите, чтобы включить или выключить):"
    if "list" not in perms:
        text += "\n\nБез «Список и поиск» админ видит в списке и поиске только билеты, которые выдал сам."
    rows = []
    for key, title in PERMS.items():
        mark = "✅" if key in perms else "▫️"
        if not access.can(key):
            mark = "🔒"  # нельзя выдать разрешение, которого нет у самого
        rows.append([(f"{mark} {title}", f"adm:t:{a['id']}:{key}")])
    rows.append([("🗑 Убрать админа", f"adm:del:{a['id']}"), ("⬅️ Готово", "adm:list")])
    return text, inline(*rows)


async def notify_admin(bot: Bot, db: Database, a, text: str) -> bool:
    """Присылает админу новое меню (например, после смены разрешений)."""
    if not a["user_id"]:
        return False
    target = Access(a["user_id"], False, parse_perms(a["perms"]))
    try:
        await Screen(bot, db, a["user_id"]).menu(text, target)
        await sync_menu_button(bot, a["user_id"])
        return True
    except TelegramAPIError:
        return False


@router.message(F.text == BTN_ADMINS, Perm("admins"))
async def admins_menu(
    message: Message, state: FSMContext, db: Database, config: Config, screen: Screen
):
    await state.clear()
    await screen.show(*admins_view(db, config))


@router.callback_query(F.data == "adm:list", Perm("admins"))
async def admins_back(
    cb: CallbackQuery, bot: Bot, state: FSMContext, db: Database, config: Config,
    screen: Screen,
):
    # Если разрешения поменялись — отправляем админу обновлённое меню.
    data = await state.get_data()
    await state.clear()
    a = db.get_admin(data.get("edit_id", 0))
    if a and a["perms"] != data.get("edit_perms"):
        await notify_admin(bot, db, a, "Ваши разрешения изменены — меню обновлено.")
    await cb.answer()
    await screen.show(*admins_view(db, config))


@router.callback_query(F.data.startswith("adm:edit:"), Perm("admins"))
async def admin_edit(cb: CallbackQuery, state: FSMContext, db: Database, access: Access, screen: Screen):
    a = db.get_admin(int(cb.data.rsplit(":", 1)[1]))
    if not a:
        return await cb.answer("Админ не найден", show_alert=True)
    await state.clear()
    await state.update_data(edit_id=a["id"], edit_perms=a["perms"])
    await cb.answer()
    await screen.show(*admin_view(a, access))


def check_editable(a, access: Access) -> str | None:
    if not a:
        return "Админ не найден"
    if a["user_id"] == access.user_id:
        return "Свои разрешения менять нельзя"
    return None


@router.callback_query(F.data.startswith("adm:t:"), Perm("admins"))
async def admin_toggle(cb: CallbackQuery, db: Database, access: Access):
    _, _, raw_id, key = cb.data.split(":")
    a = db.get_admin(int(raw_id))
    problem = check_editable(a, access)
    if not problem and (key not in PERMS or not access.can(key)):
        problem = "Вы не можете выдать разрешение, которого нет у вас"
    if problem:
        return await cb.answer(problem, show_alert=True)
    perms = set(parse_perms(a["perms"]))
    perms.symmetric_difference_update({key})
    db.set_admin_perms(a["id"], dump_perms(perms))
    text, kb = admin_view(db.get_admin(a["id"]), access)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer(f"{PERMS[key]}: {'включено' if key in perms else 'выключено'}")


@router.callback_query(F.data.startswith("adm:del:"), Perm("admins"))
async def admin_remove(
    cb: CallbackQuery, bot: Bot, state: FSMContext, db: Database, config: Config,
    access: Access, screen: Screen,
):
    a = db.get_admin(int(cb.data.rsplit(":", 1)[1]))
    problem = check_editable(a, access)
    if problem:
        return await cb.answer(problem, show_alert=True)
    db.remove_admin(a["id"])
    if a["user_id"]:
        await sync_menu_button(bot, a["user_id"], admin=False)
    await state.clear()
    await cb.answer("Админ убран")
    await screen.show(*admins_view(db, config))


@router.callback_query(F.data == "adm:add", Perm("admins"))
async def admin_add_start(cb: CallbackQuery, state: FSMContext, screen: Screen):
    await state.clear()
    await state.set_state(AddAdmin.waiting)
    await cb.answer()
    await screen.show(ADD_PROMPT, cancel_kb())


@router.message(AddAdmin.waiting, PLAIN_TEXT, Perm("admins"))
async def admin_add_finish(
    message: Message, state: FSMContext, bot: Bot, db: Database, config: Config,
    access: Access, screen: Screen,
):
    raw = message.text.strip()
    user_id = username = None
    nick = re.fullmatch(r"(?:https?://)?(?:t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})", raw)
    if re.fullmatch(r"\d{1,15}", raw):
        user_id = int(raw)
    elif nick:
        username = nick.group(1).lower()
    else:
        return await screen.show("⚠️ Не похоже на ник или ID.\n\n" + ADD_PROMPT, cancel_kb())

    if (user_id and config.is_main(user_id)) or db.admin_exists(user_id, username):
        return await screen.show("⚠️ Этот человек уже администратор.\n\n" + ADD_PROMPT, cancel_kb())

    name = f"@{username}" if username else f"ID {user_id}"
    if user_id:
        try:  # получится, если человек уже открывал бота
            chat = await bot.get_chat(user_id)
            name = chat.full_name or name
            username = chat.username.lower() if chat.username else None
        except TelegramAPIError:
            pass
    # Новому админу можно выдать только те разрешения, что есть у добавляющего.
    perms = dump_perms({p for p in parse_perms(DEFAULT_PERMS) if access.can(p)})
    admin_id = db.add_admin(user_id, username, name, perms)
    a = db.get_admin(admin_id)
    await notify_admin(bot, db, a, "Вас назначили администратором билетов.")
    await state.clear()
    await state.update_data(edit_id=a["id"], edit_perms=a["perms"])
    await screen.show(*admin_view(a, access))


# ---------- полный сброс ----------


@router.message(F.text == BTN_RESET, Perm("reset"))
async def reset_ask(message: Message, state: FSMContext, db: Database, screen: Screen):
    await state.clear()
    n = len(db.all_tickets())
    if not n:
        return await screen.show("🗑 Список и так пуст.")
    await screen.show(
        f"⚠️ <b>Полный сброс списка клиентов</b>\n\nБудет удалено билетов: <b>{n}</b>. "
        "Все выданные QR-коды перестанут работать, нумерация начнётся заново.\n"
        "Перед удалением я пришлю резервную копию списка в TXT.\n\nУдалить?",
        inline([("❗️ Да, удалить всё", "reset:yes"), ("Нет", "reset:no")]),
    )


@router.callback_query(F.data == "reset:yes", Perm("reset"))
async def reset_do(cb: CallbackQuery, db: Database, config: Config, screen: Screen):
    if db.all_tickets():
        # Сначала резервная копия: если она не отправится, ничего не удаляем.
        await screen.document(
            export_file(db, config), caption="Резервная копия списка перед сбросом"
        )
    n = db.reset_tickets()
    await cb.answer()
    await screen.show(f"🗑 Список сброшен. Удалено билетов: {n}.")


@router.callback_query(F.data == "reset:no", Perm("reset"))
async def reset_cancel(cb: CallbackQuery, screen: Screen):
    await cb.answer()
    await screen.show("Сброс отменён, список не тронут.")


# ---------- дизайн билета ----------

REVIEW_KB = inline(
    [("✅ Сохранить", "dsn:save")],
    [("🔧 Сделано неправильно", "dsn:wrong"), ("❌ Отмена", "dsn:cancel")],
)
FIX_KB = inline([("🔄 Попросить ИИ ещё раз", "dsn:retry")], [("❌ Отмена", "dsn:cancel")])


def preview(path, box: Box, bot_username: str) -> BufferedInputFile:
    return BufferedInputFile(render_preview(path, box, bot_username), filename="preview.png")


@router.message(F.text == BTN_DESIGN, Perm("design"))
async def design_menu(
    message: Message, state: FSMContext, db: Database, config: Config,
    bot_username: str, screen: Screen,
):
    await state.clear()
    path, box = current_template(db, config)
    custom = path == config.template_path
    rows = [[("🖼 Загрузить новый дизайн", "dsn:new")]]
    if custom:
        rows.append([("↩️ Вернуть стандартный", "dsn:default")])
    await screen.photo(
        await asyncio.to_thread(preview, path, box, bot_username),
        f"Текущий дизайн билета ({'свой' if custom else 'стандартный'}), "
        "QR показан для примера.",
        inline(*rows),
    )


@router.callback_query(F.data == "dsn:default", Perm("design"))
async def design_default(cb: CallbackQuery, db: Database, config: Config, screen: Screen):
    db.delete_setting("template_box")
    config.template_path.unlink(missing_ok=True)
    await cb.answer()
    await screen.show("Возвращён стандартный дизайн билета.")


@router.callback_query(F.data == "dsn:new", Perm("design"))
async def design_new(cb: CallbackQuery, state: FSMContext, screen: Screen):
    await state.set_state(Design.upload)
    await cb.answer()
    await screen.show(
        "Пришлите картинку нового дизайна билета.\n\n"
        "Лучше отправить её <b>как файл</b> (скрепка → «Файл»), чтобы Telegram не сжал "
        "качество. ИИ найдёт место под QR-код и покажет, как будет выглядеть билет.",
        inline([("❌ Отмена", "dsn:cancel")]),
    )


@router.message(Design.upload, F.photo | F.document, Perm("design"))
async def design_upload(
    message: Message, state: FSMContext, bot: Bot, config: Config,
    bot_username: str, screen: Screen,
):
    file_id = image_file_id(message)
    problem = None if file_id else "Это не картинка."
    if file_id:
        try:
            raw = await download(bot, file_id)
            await asyncio.to_thread(save_template, raw, config.pending_path)
        except (UnidentifiedImageError, OSError):
            problem = "Не удалось открыть картинку."
    if problem:
        return await screen.show(
            f"⚠️ {problem} Пришлите PNG или JPG.", inline([("❌ Отмена", "dsn:cancel")])
        )
    await propose(state, screen, config, bot_username)


async def propose(
    state: FSMContext,
    screen: Screen,
    config: Config,
    bot_username: str,
    previous: Box | None = None,
    hint: str | None = None,
):
    """Определяет место под QR (ИИ + поиск образца QR на картинке) и шлёт предпросмотр."""
    size = await asyncio.to_thread(image_size, config.pending_path)
    await screen.show("🤖 ИИ ищет место для QR-кода…")
    box, source, problem = await locate_box(config, size, previous, hint)

    if box is None:
        await state.set_state(Design.fix)
        await state.update_data(box=previous)
        return await screen.show(
            f"{escape(problem)}\n\nУкажите место вручную — три числа через пробел: "
            f"<code>x y размер</code> (в пикселях, картинка {size[0]}×{size[1]}, "
            "x и y — левый верхний угол квадрата).",
            FIX_KB,
        )
    await show_preview(state, screen, config, bot_username, box, source)


async def show_preview(
    state: FSMContext, screen: Screen, config: Config, bot_username: str,
    box: Box, source: str,
):
    await state.set_state(Design.review)
    await state.update_data(box=list(box))
    note = ""
    if box[2] < 120:
        note = "\n⚠️ QR получается мелким — может плохо сканироваться."
    await screen.photo(
        await asyncio.to_thread(preview, config.pending_path, box, bot_username),
        f"{source}: x={box[0]}, y={box[1]}, размер={box[2]} px.{note}\n\n"
        "Так будет выглядеть билет. Всё верно?",
        REVIEW_KB,
    )


@router.callback_query(Design.review, F.data == "dsn:save", Perm("design"))
async def design_save(
    cb: CallbackQuery, state: FSMContext, db: Database, config: Config, screen: Screen
):
    box = (await state.get_data()).get("box")
    if not box or not config.pending_path.exists():
        await state.clear()
        return await cb.answer("Черновик дизайна не найден, загрузите заново", show_alert=True)
    shutil.move(config.pending_path, config.template_path)
    db.set_setting("template_box", box)
    await state.clear()
    await cb.answer("Сохранено")
    await screen.show(
        "✅ Новый дизайн сохранён. Все следующие билеты (и повторная отправка старых) "
        "будут в этом дизайне."
    )


@router.callback_query(Design.review, F.data == "dsn:wrong", Perm("design"))
async def design_wrong(cb: CallbackQuery, state: FSMContext, config: Config, screen: Screen):
    await state.set_state(Design.fix)
    w, h = image_size(config.pending_path)
    await cb.message.edit_reply_markup()
    await cb.answer()
    # Предпросмотр остаётся на экране, чтобы было видно, что исправлять.
    await screen.show(
        "Что не так? Варианты:\n\n"
        "• напишите словами, что исправить (например: «QR должен быть в жёлтой части "
        "справа») — ИИ попробует ещё раз;\n"
        f"• или укажите место точно: <code>x y размер</code> в пикселях "
        f"(картинка {w}×{h}, x и y — левый верхний угол квадрата);\n"
        "• или просто попросите ИИ попробовать снова.",
        FIX_KB,
        add=True,
    )


def _prev_box(data: dict) -> Box | None:
    return tuple(data["box"]) if data.get("box") else None


@router.callback_query(Design.fix, F.data == "dsn:retry", Perm("design"))
async def design_retry(
    cb: CallbackQuery, state: FSMContext, config: Config, bot_username: str, screen: Screen
):
    await cb.answer()
    await propose(state, screen, config, bot_username, _prev_box(await state.get_data()))


@router.message(Design.fix, PLAIN_TEXT, Perm("design"))
async def design_fix(
    message: Message, state: FSMContext, config: Config, bot_username: str, screen: Screen
):
    nums = re.fullmatch(r"\s*(\d+)[\s,;]+(\d+)[\s,;]+(\d+)\s*", message.text)
    if nums:
        w, h = image_size(config.pending_path)
        box = clamp_box(tuple(int(n) for n in nums.groups()), w, h)
        return await show_preview(state, screen, config, bot_username, box, "Место задано вручную")
    previous = _prev_box(await state.get_data())
    await propose(state, screen, config, bot_username, previous, hint=message.text)


@router.callback_query(F.data.startswith("dsn:"), Perm("design"))
async def design_cancel(cb: CallbackQuery, state: FSMContext, config: Config, screen: Screen):
    """Отмена, а также нажатия на устаревшие кнопки предпросмотра."""
    current = await state.get_state()
    if current and current.startswith("Design:"):
        await state.clear()
        config.pending_path.unlink(missing_ok=True)
    await cb.answer("Отменено")
    await screen.show("Смена дизайна отменена, действует прежний.")
