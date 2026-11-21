"""Чат администратора почти пустой: всё управление — в приложении Mini App.

В чат приходят билеты и файлы (после выдачи, по запросу из приложения), оплаты на проверку и
сообщения об обновлении проекта. Сюда попадает всё остальное, что админ напишет боту: в ответ
бот подсказывает, где приложение. Клиентов (не админов) обслуживает магазин — handlers/shop.py.
"""
from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from .. import ui
from ..service import sync_menu_button
from .shop import IsAdmin

router = Router()
router.message.filter(IsAdmin())

HINT = (
    "Всё управление ботом — в приложении <b>Mini App</b>: кнопка «Mini App» слева от поля ввода "
    "сообщения. Сюда бот присылает выданные билеты, файлы и оплаты на проверку."
)
UNAVAILABLE = (
    "\n\n⚠️ Приложение сейчас недоступно (не поднялся веб-сервер или туннель) — "
    "причина в журнале бота."
)
# Кнопки клавиатуры из прежних версий: на них отвечаем, убирая клавиатуру.
OLD_BUTTONS = {
    "🎟 Новый билет", "📷 Сканировать", "📋 Список", "🔎 Поиск", "📊 Статистика",
    "👥 Админы", "🎨 Дизайн билета", "🗑 Сброс списка", "⚙️ Настройки",
}


async def reply_hint(message: Message, bot: Bot) -> None:
    await sync_menu_button(bot, message.from_user.id)
    text = HINT + ("" if ui.miniapp_url else UNAVAILABLE)
    if message.text in OLD_BUTTONS:
        return await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛒 Посмотреть магазин глазами клиента", callback_data="shop:home")
        ]]),
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
