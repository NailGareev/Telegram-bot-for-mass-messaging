"""Магазин в чате: клиент выбирает билет, платит по ссылке Kaspi, присылает PDF-чек и данные.

Шаги клиента хранятся в базе (статус заказа), а не в памяти: после перезапуска бота покупка
продолжается с того же места. Статусы: chosen → receipt → name → phone → univ → pending.
"""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import suppress
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from .. import receipts, shop
from ..access import Access
from ..config import Config
from ..db import Database, now_iso
from ..events import Hub
from ..importer import clean, normalize_phone
from ..service import current_template, render_preview
from ..shop import money

router = Router()

STATUS_TEXT = {
    "topup": "💳 нужна доплата",
    "pending": "⏳ на проверке",
    "approved": "✅ оплата принята",
    "rejected": "❌ не принята",
}


class IsAdmin(BaseFilter):
    async def __call__(self, event, access: Access | None) -> bool:
        return access is not None


class HasOrder(BaseFilter):
    """Пропускает, если у клиента есть незавершённый заказ на одном из шагов, и отдаёт его в обработчик."""

    def __init__(self, *statuses: str):
        self.statuses = statuses

    async def __call__(self, event, db: Database):
        order = db.active_order(event.from_user.id)
        return {"order": order} if order and order["status"] in self.statuses else False


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


# ---------- каталог ----------


def catalog_view(db: Database, user_id: int):
    rows = []
    if not shop.shop_enabled(db):
        text = "🎟 <b>Билеты</b>\n\nПродажа билетов сейчас закрыта."
    else:
        overview = [i for i in db.types_overview() if i["row"]["active"]]
        if not overview:
            text = "🎟 <b>Билеты</b>\n\nПродажа билетов скоро откроется."
        else:
            text = f"🎟 <b>Билеты</b>\n\n{escape(shop.welcome_text(db))}\n\nВыберите билет:"
        for item in overview:
            t = item["row"]
            if item["left"] == 0:
                rows.append([button(f"{t['title']} — {money(t['price'])} · распродано", "shop:sold")])
                continue
            tail = "" if item["left"] is None else f" · осталось {item['left']}"
            rows.append([button(f"{t['title']} — {money(t['price'])}{tail}", f"shop:t:{t['id']}")])
    for o in db.orders_of_user(user_id, 6):
        if o["status"] == "topup":
            rows.append([button(f"💳 Доплатить заказ №{o['id']}", f"shop:topup:{o['id']}")])
    if db.orders_of_user(user_id, 1):
        rows.append([button("🧾 Мои заказы", "shop:orders")])
    return text, (kb(*rows) if rows else None)


async def send_catalog(bot: Bot, db: Database, user_id: int) -> None:
    text, markup = catalog_view(db, user_id)
    await bot.send_message(user_id, text, reply_markup=markup)


async def drop(cb: CallbackQuery) -> None:
    """Убирает сообщение, на котором нажали кнопку, — чат клиента остаётся чистым."""
    with suppress(TelegramAPIError):
        await cb.message.delete()


@router.message(CommandStart(), ~IsAdmin())
async def start(message: Message, bot: Bot, db: Database):
    db.cancel_flow_orders(message.from_user.id)   # начали заново — незавершённый заказ не нужен
    await send_catalog(bot, db, message.from_user.id)


@router.callback_query(F.data == "shop:home")
async def home(cb: CallbackQuery, bot: Bot, db: Database):
    await cb.answer()
    db.cancel_flow_orders(cb.from_user.id)
    await drop(cb)
    await send_catalog(bot, db, cb.from_user.id)


@router.callback_query(F.data == "shop:sold")
async def sold_out(cb: CallbackQuery):
    await cb.answer("Эти билеты распроданы", show_alert=True)


# ---------- выбор билета и оплата ----------


@router.callback_query(F.data.regexp(r"^shop:t:\d+$"))
async def choose(cb: CallbackQuery, bot: Bot, db: Database, config: Config, bot_username: str):
    t = db.get_type(int(cb.data.rsplit(":", 1)[1]))
    if not shop.shop_enabled(db):
        return await cb.answer("Продажа билетов сейчас закрыта", show_alert=True)
    if not t or not t["active"]:
        return await cb.answer("Этот билет сейчас недоступен", show_alert=True)
    if db.type_left(t["id"]) == 0:
        return await cb.answer("Эти билеты распроданы", show_alert=True)
    await cb.answer()
    user = cb.from_user
    db.cancel_flow_orders(user.id)
    order_id = db.create_order(user.id, user.username, user.full_name, t)
    await drop(cb)

    path, box, name = current_template(db, config, t["id"])
    png = await asyncio.to_thread(render_preview, path, box, bot_username, name)
    url = shop.pay_url(db, t)
    left = db.type_left(t["id"])
    caption = f"🎫 <b>{escape(t['title'])}</b> — {money(t['price'])}\n"
    if t["description"]:
        caption += f"{escape(t['description'])}\n"
    if left is not None:
        caption += f"Осталось билетов: {left}\n"
    caption += "\n(так будет выглядеть билет; QR и имя в примере условные)\n\n"
    rows = []
    if url:
        caption += (
            "1. Нажмите «Оплатить через Kaspi» и оплатите билет.\n"
            "2. Вернитесь сюда и нажмите «Я оплатил(а)».\n"
            "3. Пришлите PDF-чек и свои данные."
        )
        rows.append([InlineKeyboardButton(text="💳 Оплатить через Kaspi", url=url)])
        rows.append([button("✅ Я оплатил(а)", f"shop:paid:{order_id}")])
    else:
        caption += "⚠️ Ссылка на оплату пока не настроена. Попробуйте позже или обратитесь к организатору."
    rows.append([button("⬅️ Назад", "shop:home")])
    await bot.send_photo(
        user.id, BufferedInputFile(png, filename="ticket.png"), caption=caption, reply_markup=kb(*rows)
    )


def own_order(db: Database, cb: CallbackQuery):
    order = db.get_order(int(cb.data.rsplit(":", 1)[1]))
    return order if order and order["user_id"] == cb.from_user.id else None


CANCEL_ROW = lambda order_id: [button("❌ Отменить заказ", f"shop:cancel:{order_id}")]  # noqa: E731


@router.callback_query(F.data.regexp(r"^shop:paid:\d+$"))
async def paid(cb: CallbackQuery, bot: Bot, db: Database):
    order = own_order(db, cb)
    if not order or order["status"] not in ("chosen", "receipt"):
        return await cb.answer("Этот заказ уже не актуален — нажмите /start", show_alert=True)
    await cb.answer()
    db.update_order(order["id"], status="receipt")
    with suppress(TelegramAPIError):
        await cb.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(
        cb.from_user.id,
        "📎 Пришлите <b>PDF-чек</b> об оплате <b>файлом</b>.\n\n"
        "В Kaspi: Платежи → нужный платёж → Чек → «Поделиться» → PDF. "
        "Скриншот не подойдёт — нужен именно PDF.",
        reply_markup=kb(CANCEL_ROW(order["id"])),
    )


@router.callback_query(F.data.regexp(r"^shop:cancel:\d+$"))
async def cancel(cb: CallbackQuery, bot: Bot, db: Database):
    order = own_order(db, cb)
    if order and order["status"] == "topup":
        return await cb.answer(
            "По этому заказу уже внесены деньги — отменить его может только организатор.", show_alert=True
        )
    if order and order["status"] in db.FLOW:
        db.update_order(order["id"], status="cancelled")
    await cb.answer("Заказ отменён")
    await drop(cb)
    await bot.send_message(cb.from_user.id, "Заказ отменён.", reply_markup=ReplyKeyboardRemove())
    await send_catalog(bot, db, cb.from_user.id)


# ---------- чек ----------


def is_pdf(doc) -> bool:
    return (doc.mime_type == "application/pdf") or (doc.file_name or "").lower().endswith(".pdf")


@router.message(F.document, HasOrder("receipt", "topup"))
async def receipt_file(
    message: Message, bot: Bot, db: Database, config: Config, order, hub: Hub
):
    doc = message.document
    if not is_pdf(doc):
        return await message.answer("Нужен именно PDF-файл с чеком. Пришлите его как файл (скрепка → «Файл»).")
    if doc.file_size and doc.file_size > receipts.MAX_PDF_BYTES:
        return await message.answer("Файл больше 5 МБ — это не похоже на чек. Пришлите PDF-чек из Kaspi.")
    wait = await message.answer("🔎 Проверяю чек…")
    try:
        buf = io.BytesIO()
        await bot.download(doc.file_id, destination=buf)
        raw = buf.getvalue()
        receipt = await asyncio.to_thread(receipts.parse_pdf, raw)
    except receipts.ReceiptError as e:
        with suppress(TelegramAPIError):
            await wait.delete()
        return await message.answer(f"⚠️ {escape(str(e))} Пришлите PDF-чек из Kaspi.")
    except TelegramAPIError:
        with suppress(TelegramAPIError):
            await wait.delete()
        return await message.answer("Не удалось получить файл. Пришлите чек ещё раз.")
    with suppress(TelegramAPIError):
        await wait.delete()

    block = shop.check_new_receipt(db, order, receipt)
    if block:
        return await message.answer(f"⚠️ {escape(block)}")

    config.receipts_dir.mkdir(parents=True, exist_ok=True)
    name = shop.new_receipt_name(order["id"])
    (config.receipts_dir / name).write_bytes(raw)
    db.add_receipt(order["id"], name, receipt.number, receipt.amount, json.dumps(receipt.to_dict(), ensure_ascii=False))
    order = db.get_order(order["id"])
    paid, _, short = shop.coverage(db, order)
    if short > 0:
        # Клиент ошибся с суммой: заказ ждёт доплаты, чеки складываются
        db.update_order(order["id"], status="topup")
        got = f"✅ Чек получен: {money(receipt.amount)}." if receipt.amount is not None else "✅ Чек получен."
        await shop.notify_topup(bot, db, db.get_order(order["id"]), got)
        return
    intro = "✅ Чек получен." + (f" Оплачено {money(paid)} из {money(order['price'])}." if paid and paid != order["price"] else "")
    if order["full_name"] and order["phone"] and order["university"]:   # повторный чек: данные уже есть
        return await submit(bot, db, config, order, hub)
    db.update_order(order["id"], status="name")
    await message.answer(
        f"{intro}\n\nТеперь введите ваши <b>фамилию и имя</b>:",
        reply_markup=kb(CANCEL_ROW(order["id"])),
    )


@router.message(HasOrder("receipt", "topup"))
async def receipt_not_file(message: Message):
    await message.answer(
        "Жду PDF-чек. Пришлите его <b>файлом</b> (скрепка → «Файл»). Скриншот или фото не подойдут."
    )


# ---------- данные клиента ----------

CONTACT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
    resize_keyboard=True, one_time_keyboard=True,
)
TEXT = F.text & ~F.text.startswith("/")


@router.message(TEXT, HasOrder("name"))
async def got_name(message: Message, db: Database, order):
    name = clean(message.text)
    if len(name) < 3 or " " not in name:
        return await message.answer("Введите фамилию и имя полностью, например: <i>Иванов Иван</i>")
    db.update_order(order["id"], full_name=name, status="phone")
    await message.answer(
        "Введите <b>номер телефона</b> (например, +7 707 123 45 67) или нажмите кнопку ниже:",
        reply_markup=CONTACT_KB,
    )


@router.message(F.contact | TEXT, HasOrder("phone"))
async def got_phone(message: Message, db: Database, order):
    raw = message.contact.phone_number if message.contact else message.text
    phone = normalize_phone(raw)
    if not phone:
        return await message.answer("Не похоже на номер телефона. Введите ещё раз, например +7 707 123 45 67:")
    db.update_order(order["id"], phone=phone, status="univ")
    await message.answer("Введите <b>университет</b>:", reply_markup=ReplyKeyboardRemove())


@router.message(TEXT, HasOrder("univ"))
async def got_university(
    message: Message, bot: Bot, db: Database, config: Config, order, hub: Hub
):
    university = clean(message.text)
    if len(university) < 2:
        return await message.answer("Введите название университета:")
    db.update_order(order["id"], university=university)
    await submit(bot, db, config, db.get_order(order["id"]), hub)


async def submit(bot: Bot, db: Database, config: Config, order, hub: Hub) -> None:
    """Все данные собраны: заказ уходит на проверку администраторам."""
    db.update_order(
        order["id"], status="pending", submitted_at=now_iso(), reject_reason="",
        reviewed_at=None, reviewed_by="",
    )
    order = db.get_order(order["id"])
    await bot.send_message(
        order["user_id"],
        "✅ <b>Оплата отправлена на проверку</b>\n\n"
        f"Заказ №{order['id']} · {escape(order['type_title'])} · {money(order['price'])}\n"
        f"👤 {escape(order['full_name'])}\n📞 {escape(order['phone'])}\n🎓 {escape(order['university'])}\n\n"
        "Как только оплату проверят, билет придёт в этот чат.",
        reply_markup=kb([button("🧾 Мои заказы", "shop:orders")]),
    )
    await shop.notify_reviewers(bot, db, config, order, hub)


@router.message(HasOrder("chosen"))
async def only_buttons(message: Message):
    await message.answer("Когда оплатите билет, нажмите «✅ Я оплатил(а)» под сообщением с билетом.")


# ---------- мои заказы ----------


@router.callback_query(F.data == "shop:orders")
async def my_orders(cb: CallbackQuery, bot: Bot, db: Database):
    await cb.answer()
    orders = db.orders_of_user(cb.from_user.id)
    await drop(cb)
    if not orders:
        return await send_catalog(bot, db, cb.from_user.id)
    lines, rows = ["🧾 <b>Мои заказы</b>\n"], []
    for o in orders:
        line = f"№{o['id']} · {escape(o['type_title'])} · {money(o['price'])} — {STATUS_TEXT[o['status']]}"
        if o["status"] == "rejected" and o["reject_reason"]:
            line += f"\n   Причина: {escape(o['reject_reason'])}"
        lines.append(line)
        if o["status"] == "topup":
            rows.append([button(f"💳 Доплатить (№{o['id']})", f"shop:topup:{o['id']}")])
        if o["status"] == "approved" and o["ticket_id"]:
            rows.append([button(f"🎟 Прислать билет №{o['id']}", f"shop:send:{o['id']}")])
        if o["status"] == "rejected":
            rows.append([button(f"📎 Отправить чек заново (№{o['id']})", f"shop:retry:{o['id']}")])
    rows.append([button("⬅️ К билетам", "shop:home")])
    await bot.send_message(cb.from_user.id, "\n".join(lines), reply_markup=kb(*rows))


@router.callback_query(F.data.regexp(r"^shop:send:\d+$"))
async def resend(
    cb: CallbackQuery, bot: Bot, db: Database, config: Config, bot_username: str
):
    order = own_order(db, cb)
    ticket = db.get_ticket(order["ticket_id"]) if order and order["status"] == "approved" and order["ticket_id"] else None
    if not ticket:
        return await cb.answer("Билет не найден", show_alert=True)
    await cb.answer("Отправляю билет…")
    await shop.deliver_ticket(bot, db, config, bot_username, order, ticket)


@router.callback_query(F.data.regexp(r"^shop:topup:\d+$"))
async def topup(cb: CallbackQuery, bot: Bot, db: Database):
    order = own_order(db, cb)
    if not order or order["status"] != "topup":
        return await cb.answer("Этот заказ уже не ждёт доплаты", show_alert=True)
    await cb.answer()
    db.cancel_flow_orders(cb.from_user.id)   # чтобы этот заказ стал активным
    await drop(cb)
    await shop.notify_topup(bot, db, order)


@router.callback_query(F.data.regexp(r"^shop:retry:\d+$"))
async def retry(cb: CallbackQuery, bot: Bot, db: Database):
    order = own_order(db, cb)
    if not order or order["status"] != "rejected":
        return await cb.answer("Этот заказ уже не актуален", show_alert=True)
    await cb.answer()
    db.cancel_flow_orders(cb.from_user.id)
    db.update_order(order["id"], status="receipt")
    await bot.send_message(
        cb.from_user.id,
        "📎 Пришлите правильный <b>PDF-чек</b> файлом — я отправлю заказ на проверку ещё раз.",
        reply_markup=kb(CANCEL_ROW(order["id"])),
    )


# ---------- всё остальное ----------


@router.message(~IsAdmin())
async def anything(message: Message, bot: Bot, db: Database):
    await send_catalog(bot, db, message.from_user.id)


@router.callback_query(~IsAdmin())
async def stale(cb: CallbackQuery):
    await cb.answer("Эта кнопка уже не работает. Нажмите /start.", show_alert=True)
