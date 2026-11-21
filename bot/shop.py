"""Продажа билетов через бота: каталог, сверка чека, выдача билета после оплаты.

Клиент выбирает тип билета, платит по ссылке Kaspi, присылает PDF-чек и данные. Заказ попадает
в «Оплаты на проверку»; когда администратор принимает оплату, бот выдаёт билет (его выдала
«Система») и присылает клиенту в чат.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from . import ui
from .access import parse_perms
from .config import Config
from .db import Database, Ticket, now_iso
from .receipts import Receipt
from .service import ticket_png
from .ui import client_block, dash, ticket_code

SYSTEM_ID, SYSTEM_NAME = 0, "Система"      # так в статистике и списках подписаны билеты, купленные в боте
DEFAULT_WELCOME = (
    "Выберите билет, оплатите его через Kaspi и пришлите сюда PDF-чек — после проверки "
    "билет придёт в этот чат."
)


class ShopError(Exception):
    """Понятная администратору причина, по которой действие не выполнено."""


def money(amount: int | None) -> str:
    return "—" if amount is None else f"{amount:,}".replace(",", " ") + " ₸"


# ---------- настройки магазина (задаёт главный админ) ----------


def shop_enabled(db: Database) -> bool:
    return db.get_setting("shop_enabled", True) is not False


def welcome_text(db: Database) -> str:
    return (db.get_setting("shop_welcome") or "").strip() or DEFAULT_WELCOME


def pay_url(db: Database, type_row) -> str:
    """Ссылка оплаты: своя у типа билета, иначе общая."""
    return (type_row["pay_url"] or db.get_setting("shop_pay_url") or "").strip()


def seller_bin(db: Database, type_row=None) -> str:
    """ИИН/БИН получателя для сверки чека — тот, что задан вместе со ссылкой оплаты:
    у типа со своей ссылкой — его БИН (пусто — проверка получателя выключена), иначе общий."""
    if type_row is not None and type_row["pay_url"]:
        return (type_row["pay_bin"] or "").strip()
    return (db.get_setting("shop_bin") or "").strip()


def coverage(db: Database, order) -> tuple[int, int, int]:
    """(внесено по чекам, чеков с нераспознанной суммой, сколько ещё не хватает).
    Если у какого-то чека сумма не распознана, недостачу автоматически не считаем —
    такой заказ решит админ по картинке чека."""
    paid, unknown = db.paid_total(order["id"])
    short = 0 if unknown else max(0, order["price"] - paid)
    return paid, unknown, short


def new_receipt_name(order_id: int) -> str:
    return f"order_{order_id}_{secrets.token_hex(4)}.pdf"


# ---------- проверка чека ----------


def check_new_receipt(db: Database, order, receipt: Receipt) -> str | None:
    """Жёсткие отказы при загрузке чека клиентом (текст показывается клиенту): чек уже
    использован или выписан не на нашего получателя. Недоплата не отказ — клиент может
    доплатить остаток вторым чеком; прочее попадает в отметки для администратора."""
    if receipt.number:
        if db.order_has_receipt(order["id"], receipt.number):
            return "Этот чек вы уже присылали. Пришлите чек за доплату."
        if db.find_receipt(receipt.number, order["id"]):
            return "Этот чек уже использован для другого заказа. Пришлите чек за ваш платёж."
    wanted = seller_bin(db, db.get_type(order["type_id"]))
    if wanted and receipt.seller_bin and receipt.seller_bin != wanted:
        return "Чек выписан не на нашего получателя. Оплатите по ссылке из бота и пришлите этот чек."
    return None


def review_flags(db: Database, config: Config, order, overview=None) -> list[dict]:
    """Результат автоматической сверки чеков с заказом для администратора:
    [{level: ok|warn|bad, text}]."""
    flags: list[dict] = []

    def add(level: str, text: str) -> None:
        flags.append({"level": level, "text": text})

    receipts = db.receipts_of(order["id"])
    if not receipts:
        add("bad", "Чек не загружен")
        return flags
    price = order["price"]
    paid, unknown, _ = coverage(db, order)
    many = len(receipts) > 1
    if unknown:
        add("warn", f"Сумма не распознана у чеков: {unknown} — сверьте по картинке")
    if paid or not unknown:
        count = f" (чеков: {len(receipts)})" if many else ""
        if paid == price:
            add("ok", f"Оплачено {money(paid)}{count} — совпадает с ценой билета")
        elif paid > price:
            add("warn", f"Оплачено {money(paid)}{count}, цена билета {money(price)} — переплата {money(paid - price)}")
        elif unknown:
            add("warn", f"Распознано {money(paid)} из {money(price)}")
        else:
            add("bad", f"Оплачено {money(paid)}{count} из {money(price)} — не хватает {money(price - paid)}")
    wanted = seller_bin(db, db.get_type(order["type_id"]))
    for r in receipts:
        info = Receipt.from_dict(json.loads(r["info"] or "{}"))
        label = f"Чек №{info.number}" if info.number else "Чек"
        if not info.recognized:
            add("warn", f"{label}: поля не распознаны автоматически — сверьте сумму и дату по картинке")
        if info.number:
            other = db.find_receipt(info.number, order["id"])
            if other:
                add("bad", f"{label} уже использован в заказе №{other['order_id']}")
            elif not many:
                add("ok", f"{label} раньше не использовался")
        if wanted:
            if not info.seller_bin:
                add("warn", f"{label}: ИИН/БИН получателя не распознан")
            elif info.seller_bin == wanted:
                if not many:
                    add("ok", "Получатель платежа совпадает с ИИН/БИН ссылки оплаты")
            else:
                add("bad", f"{label}: получатель (ИИН/БИН {info.seller_bin}) — не тот, что у ссылки оплаты")
        if info.recognized and not info.success:
            add("warn", f"{label}: нет отметки «Оплата совершена»")
        if info.paid_at:
            try:
                paid_at = datetime.fromisoformat(info.paid_at).replace(tzinfo=config.tz)
                created = datetime.fromisoformat(order["created_at"])
                if paid_at < created - timedelta(minutes=15):
                    add("warn", f"{label} выписан раньше заказа ({info.paid_at.replace('T', ' ')}) — возможно, старый")
                elif paid_at > datetime.now(config.tz) + timedelta(minutes=10):
                    add("warn", f"{label}: дата в будущем")
            except ValueError:
                pass
    first = Receipt.from_dict(json.loads(receipts[0]["info"] or "{}"))
    if first.buyer and order["full_name"]:
        name = first.buyer.split()[0].casefold() if first.buyer.split() else ""
        tokens = [t.casefold() for t in order["full_name"].replace(".", " ").split()]
        if name and name in tokens:
            add("ok", f"Покупатель в чеке: {first.buyer}")
        else:
            add("warn", f"Покупатель в чеке ({first.buyer}) не совпадает с ФИ клиента — часто платит родственник или друг")
    t = next((i for i in (overview or db.types_overview()) if i["row"]["id"] == order["type_id"]), None)
    if t and order["status"] == "pending" and t["row"]["quantity"] is not None:
        if t["sold"] + t["pending"] > t["row"]["quantity"]:
            add("warn", f"Лимит билетов «{t['row']['title']}» ({t['row']['quantity']}) уже исчерпан — принятие оплаты выйдет за него")
    return flags


# ---------- принятие и отклонение ----------


def approve_order(db: Database, order_id: int, who: str) -> tuple[Ticket, object]:
    """Принимает оплату: выдаёт билет от имени «Системы». Возвращает (билет, заказ)."""
    order = db.get_order(order_id)
    if not order:
        raise ShopError("Заказ не найден")
    if not db.claim_order(order_id, ("pending",), status="approved", reviewed_at=now_iso(), reviewed_by=who):
        raise ShopError("Этот заказ уже обработан другим администратором")
    try:
        ticket = db.create_ticket(
            order["full_name"], "", order["university"], order["phone"],
            SYSTEM_ID, SYSTEM_NAME, order["type_id"], order["type_title"], order_id,
        )
    except Exception:
        db.update_order(order_id, status="pending", reviewed_at=None, reviewed_by="")
        raise
    db.update_order(order_id, ticket_id=ticket["id"])
    return ticket, db.get_order(order_id)


def reject_order(db: Database, order_id: int, who: str, reason: str) -> object:
    """Оплата не принята: чеки отзываются (не засчитываются), клиент может прислать новый."""
    reason = " ".join(reason.split())[:300]
    if not db.claim_order(
        order_id, ("pending", "topup"), status="rejected", reviewed_at=now_iso(), reviewed_by=who,
        reject_reason=reason,
    ):
        raise ShopError("Этот заказ уже обработан другим администратором")
    db.void_receipts(order_id)
    return db.get_order(order_id)


def request_topup(db: Database, order_id: int, who: str) -> tuple[object, int]:
    """Админ видит недоплату: заказ уходит обратно клиенту — доплатить остаток. Чеки сохраняются."""
    order = db.get_order(order_id)
    if not order:
        raise ShopError("Заказ не найден")
    _, unknown, short = coverage(db, order)
    if short <= 0:
        raise ShopError("Недоплаты нет: сумма по чекам не меньше цены билета")
    if not db.claim_order(order_id, ("pending",), status="topup", reviewed_by=who):
        raise ShopError("Этот заказ уже обработан другим администратором")
    return db.get_order(order_id), short


# ---------- сообщения клиенту и админам ----------


async def deliver_ticket(
    bot: Bot, db: Database, config: Config, bot_username: str, order, ticket: Ticket,
) -> bool:
    """Присылает клиенту билет и данные в чат. False — не получилось (клиент мог заблокировать бота)."""
    try:
        png = await ticket_png(ticket, db, config, bot_username)
        await bot.send_document(
            order["user_id"],
            BufferedInputFile(png, filename=f"ticket_{ticket_code(config, ticket['id'])}.png"),
            caption="✅ <b>Оплата принята!</b> Ваш билет — во вложении. Покажите QR-код на входе.\n\n"
            + client_block(ticket, config),
        )
    except TelegramAPIError:
        db.update_order(order["id"], delivered=0)
        return False
    db.update_order(order["id"], delivered=1)
    return True


async def notify_topup(bot: Bot, db: Database, order, intro: str = "") -> bool:
    """Просит клиента доплатить остаток и прислать ещё один чек."""
    paid, _, short = coverage(db, order)
    url = pay_url(db, db.get_type(order["type_id"]) or {"pay_url": ""})
    text = (intro + "\n\n" if intro else "") + (
        f"💳 <b>Нужна доплата: {money(short)}</b>\n"
        f"Билет «{ui.escape(order['type_title'])}» стоит {money(order['price'])}, по вашим чекам внесено {money(paid)}.\n\n"
        f"Доплатите {money(short)} через Kaspi и пришлите ещё один PDF-чек в этот чат — "
        "суммы чеков сложатся."
    )
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="💳 Доплатить через Kaspi", url=url)])
    try:
        await bot.send_message(order["user_id"], text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    except TelegramAPIError:
        return False
    return True


async def notify_rejected(bot: Bot, order, reason: str) -> None:
    text = "❌ <b>Оплата не принята.</b>\n" + (f"Причина: {ui.escape(reason)}\n" if reason else "")
    text += "\nЕсли вы уже оплатили, пришлите правильный чек — мы проверим ещё раз."
    try:
        await bot.send_message(
            order["user_id"], text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📎 Отправить чек заново", callback_data=f"shop:retry:{order['id']}")],
            ]),
        )
    except TelegramAPIError:
        pass


def reviewer_ids(db: Database, config: Config) -> list[int]:
    """Кто проверяет оплаты: главные админы и админы с разрешением «Проверка оплат»."""
    ids = set(config.main_admin_ids)
    for a in db.list_admins():
        if a["user_id"] and "payments" in parse_perms(a["perms"]):
            ids.add(a["user_id"])
    return sorted(ids)


def app_link(order_id: int | None = None) -> str:
    """Адрес приложения (с переходом сразу к оплате, если указан заказ)."""
    url = ui.miniapp_url
    if url and order_id:
        url += ("&" if "?" in url else "?") + f"pay={order_id}"
    return url


async def notify_reviewers(bot: Bot, db: Database, config: Config, order, hub=None) -> None:
    text = (
        "🧾 <b>Новая оплата на проверку</b>\n"
        f"Заказ №{order['id']} · {ui.escape(order['type_title'])} · {money(order['price'])}\n"
        f"👤 {ui.escape(order['full_name'])} · 📞 {dash(order['phone'])} · 🎓 {dash(order['university'])}"
    )
    link = app_link(order["id"])
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть проверку", web_app=WebAppInfo(url=link))
    ]]) if link else None
    for user_id in reviewer_ids(db, config):
        if hub is not None:
            hub.publish(user_id, {"type": "notice", "text": f"Новая оплата на проверку: {order['full_name']}"})
        try:
            await bot.send_message(user_id, text, reply_markup=markup)
        except TelegramAPIError:
            pass
