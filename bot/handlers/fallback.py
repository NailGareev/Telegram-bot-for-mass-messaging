"""Всё, что не обработано выше: /id, посторонние пользователи, непонятные сообщения."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..db import Database
from ..imaging import extract_token
from ..service import Role
from ..ui import main_menu

router = Router()


@router.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Role())
async def admin_unknown(message: Message, state: FSMContext, config: Config):
    if await state.get_state():
        return await message.answer("Не понял. Отправьте то, что я просил, или нажмите «Отмена».")
    await message.answer(
        "Выберите действие в меню.",
        reply_markup=main_menu(config.is_main(message.from_user.id)),
    )


@router.callback_query(Role())
async def admin_stale_button(cb: CallbackQuery):
    await cb.answer("Кнопка устарела или недоступна", show_alert=True)


@router.message()
async def stranger(message: Message):
    if extract_token(message.text or ""):
        # Гость отсканировал свой билет обычной камерой
        return await message.answer(
            "🎟 Это билет на мероприятие. Покажите QR-код администратору на входе."
        )
    await message.answer(
        "Этот бот — для администраторов билетов.\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code> — передайте его главному "
        "администратору, если вам нужен доступ."
    )


@router.callback_query()
async def stranger_button(cb: CallbackQuery):
    await cb.answer("Нет доступа", show_alert=True)
