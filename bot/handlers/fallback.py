"""Чат с ботом почти пустой: всё управление — в приложении Mini App.

В чат приходят только билеты и файлы (после выдачи, по запросу из приложения), а также
сообщения об обновлении проекта. Сюда попадает всё остальное, что админ напишет боту:
в ответ бот подсказывает, где приложение, и убирает старую клавиатуру прежних версий.
Посторонним бот не отвечает вообще — их отсекает AccessMiddleware.
"""
from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from .. import ui
from ..service import sync_menu_button

router = Router()

HINT = (
    "Всё управление ботом — в приложении <b>Mini App</b>: кнопка «Mini App» слева от поля ввода "
    "сообщения. Сюда бот присылает только выданные билеты и файлы."
)
UNAVAILABLE = (
    "\n\n⚠️ Приложение сейчас недоступно (не поднялся веб-сервер или туннель) — "
    "причина в журнале бота."
)


async def reply_hint(message: Message, bot: Bot) -> None:
    await sync_menu_button(bot, message.from_user.id)
    await message.answer(
        HINT + ("" if ui.miniapp_url else UNAVAILABLE),
        reply_markup=ReplyKeyboardRemove(),   # убирает клавиатуру, оставшуюся от прежних версий
    )


@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    await reply_hint(message, bot)


@router.message()
async def anything(message: Message, bot: Bot):
    await reply_hint(message, bot)


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    await cb.answer("Кнопки в чате больше не работают: всё теперь в приложении Mini App.", show_alert=True)
