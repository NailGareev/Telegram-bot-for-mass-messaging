"""Всё, что не обработано разделами. Сюда попадают только администраторы:
посторонних отсекает AccessMiddleware, им бот не отвечает вообще."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..access import Access
from ..service import Screen
from ..ui import MENU_TEXTS

router = Router()


@router.message(F.text)
async def unknown_text(message: Message, state: FSMContext, access: Access, screen: Screen):
    if message.text in MENU_TEXTS:
        # Кнопка раздела, на который (уже) нет разрешения — обновляем меню.
        await state.clear()
        await screen.clear()
        return await screen.menu("Этот раздел вам недоступен. Меню обновлено.", access)
    if await state.get_state() is None:
        await screen.menu("Выберите действие в меню.", access)


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    await cb.answer("Кнопка устарела или раздел недоступен", show_alert=True)
