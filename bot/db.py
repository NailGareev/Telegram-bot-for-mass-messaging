"""Лёгкая база на SQLite: билеты, типы билетов, заказы, администраторы, настройки."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    token              TEXT NOT NULL UNIQUE,
    full_name          TEXT NOT NULL,
    study_group        TEXT NOT NULL,
    university         TEXT NOT NULL,
    phone              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    created_by         INTEGER NOT NULL,
    created_by_name    TEXT NOT NULL,
    scan_count         INTEGER NOT NULL DEFAULT 0,
    first_scan_at      TEXT,
    first_scan_by_name TEXT,
    last_scan_at       TEXT,
    type_id            INTEGER,                 -- тип билета (NULL — выдан без типа)
    type_title         TEXT NOT NULL DEFAULT '',
    order_id           INTEGER                  -- заказ, по которому выдан билет
);
CREATE TABLE IF NOT EXISTS ticket_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price       INTEGER NOT NULL DEFAULT 0,     -- в тенге
    quantity    INTEGER,                        -- сколько всего можно продать; NULL — без лимита
    active      INTEGER NOT NULL DEFAULT 1,
    pay_url     TEXT NOT NULL DEFAULT '',       -- своя ссылка оплаты; пусто — общая
    pay_bin     TEXT NOT NULL DEFAULT '',       -- ИИН/БИН получателя этой ссылки (для сверки чека)
    sort        INTEGER NOT NULL DEFAULT 0,
    has_design  INTEGER NOT NULL DEFAULT 0,     -- свой дизайн (файл data/templates/type_N.png)
    design_box  TEXT,                           -- JSON [x, y, сторона] — место QR
    design_name TEXT,                           -- JSON — раскладка ФИ
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,              -- Telegram ID покупателя
    username     TEXT,
    tg_name      TEXT NOT NULL DEFAULT '',
    type_id      INTEGER,
    type_title   TEXT NOT NULL,                 -- название и цена на момент заказа
    price        INTEGER NOT NULL,
    status       TEXT NOT NULL,                 -- chosen, receipt, topup, name, phone, univ, pending, approved, rejected, cancelled
    full_name    TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    university   TEXT NOT NULL DEFAULT '',
    receipt_file TEXT NOT NULL DEFAULT '',      -- имя файла в data/receipts
    receipt_no   TEXT NOT NULL DEFAULT '',      -- номер чека из PDF
    receipt_info TEXT NOT NULL DEFAULT '{}',    -- JSON: что распознано в чеке
    created_at   TEXT NOT NULL,
    submitted_at TEXT,
    reviewed_at  TEXT,
    reviewed_by  TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    ticket_id    INTEGER,
    delivered    INTEGER NOT NULL DEFAULT 0     -- билет дошёл до клиента в чат
);
CREATE INDEX IF NOT EXISTS orders_user ON orders (user_id, status);
CREATE INDEX IF NOT EXISTS orders_receipt ON orders (receipt_no);
CREATE TABLE IF NOT EXISTS order_receipts (    -- у заказа может быть несколько чеков (доплата)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL,
    file       TEXT NOT NULL,                   -- имя файла в data/receipts
    number     TEXT NOT NULL DEFAULT '',        -- номер чека из PDF
    amount     INTEGER,                         -- сумма из чека; NULL — не распознана
    info       TEXT NOT NULL DEFAULT '{}',      -- JSON: что распознано в чеке
    created_at TEXT NOT NULL,
    void       INTEGER NOT NULL DEFAULT 0       -- 1 — чек отозван админом и не засчитывается
);
CREATE INDEX IF NOT EXISTS order_receipts_order ON order_receipts (order_id);
CREATE INDEX IF NOT EXISTS order_receipts_number ON order_receipts (number);
CREATE TABLE IF NOT EXISTS admins (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER UNIQUE,          -- NULL, пока добавленный по нику не написал боту
    username TEXT,                    -- без @, в нижнем регистре
    name     TEXT NOT NULL,
    perms    TEXT NOT NULL,           -- разрешённые разделы через запятую
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

Ticket = sqlite3.Row


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate_admins()
        self.conn.executescript(SCHEMA)
        self._migrate_tickets()
        self._migrate_shop()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _migrate_admins(self) -> None:
        """Перенос админов из первой версии таблицы (без ников и разрешений)."""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(admins)")]
        if not cols or "perms" in cols:
            return
        with self.conn:
            self.conn.execute("ALTER TABLE admins RENAME TO admins_old")
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT INTO admins (user_id, name, perms, added_at)"
                " SELECT user_id, name, 'new,scan', added_at FROM admins_old"
            )
            self.conn.execute("DROP TABLE admins_old")

    def _migrate_tickets(self) -> None:
        """Колонки типа билета и заказа для баз, созданных до магазина."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(tickets)")}
        for name, ddl in (
            ("type_id", "INTEGER"),
            ("type_title", "TEXT NOT NULL DEFAULT ''"),
            ("order_id", "INTEGER"),
        ):
            if name not in have:
                self.conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}")

    def _migrate_shop(self) -> None:
        """Для баз, созданных до доплат и привязки ИИН/БИН к ссылке."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(ticket_types)")}
        if "pay_bin" not in have:
            self.conn.execute("ALTER TABLE ticket_types ADD COLUMN pay_bin TEXT NOT NULL DEFAULT ''")
        # Чек из старой схемы (один на заказ) переезжает в таблицу чеков.
        for o in self.conn.execute(
            "SELECT id, receipt_file, receipt_no, receipt_info, created_at FROM orders"
            " WHERE receipt_file != '' AND id NOT IN (SELECT order_id FROM order_receipts)"
        ).fetchall():
            try:
                amount = json.loads(o["receipt_info"] or "{}").get("amount")
            except ValueError:
                amount = None
            self.conn.execute(
                "INSERT INTO order_receipts (order_id, file, number, amount, info, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (o["id"], o["receipt_file"], o["receipt_no"], amount, o["receipt_info"] or "{}", o["created_at"]),
            )

    # ---------- билеты ----------

    def create_ticket(
        self,
        full_name: str,
        study_group: str,
        university: str,
        phone: str,
        created_by: int,
        created_by_name: str,
        type_id: int | None = None,
        type_title: str = "",
        order_id: int | None = None,
    ) -> Ticket:
        token = secrets.token_urlsafe(12)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tickets (token, full_name, study_group, university, phone,"
                " created_at, created_by, created_by_name, type_id, type_title, order_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (token, full_name, study_group, university, phone, now_iso(),
                 created_by, created_by_name, type_id, type_title, order_id),
            )
        return self.get_ticket(cur.lastrowid)

    def get_ticket(self, ticket_id: int) -> Ticket | None:
        return self.conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

    def get_ticket_by_token(self, token: str) -> Ticket | None:
        return self.conn.execute(
            "SELECT * FROM tickets WHERE token = ?", (token,)
        ).fetchone()

    def find_by_phone(self, phone: str) -> list[Ticket]:
        return self.conn.execute(
            "SELECT * FROM tickets WHERE phone = ? ORDER BY id", (phone,)
        ).fetchall()

    def all_tickets(self, owner: int | None = None) -> list[Ticket]:
        """Все билеты; с owner — только выданные этим администратором."""
        if owner is None:
            return self.conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM tickets WHERE created_by = ? ORDER BY id", (owner,)
        ).fetchall()

    def search(self, query: str, owner: int | None = None, limit: int = 10) -> list[Ticket]:
        """Поиск по ФИ, телефону, группе, вузу и номеру билета.

        Фильтруем в Python: LIKE в SQLite не умеет регистронезависимую кириллицу.
        """
        q = query.casefold().strip()
        digits = "".join(ch for ch in q if ch.isdigit())
        found = []
        for t in self.all_tickets(owner):
            hay = " ".join(
                (t["full_name"], t["study_group"], t["university"], t["phone"])
            ).casefold()
            phone_digits = "".join(ch for ch in t["phone"] if ch.isdigit())
            if q in hay or (len(digits) >= 4 and digits in phone_digits):
                found.append(t)
                if len(found) >= limit:
                    break
        return found

    def register_scan(self, ticket_id: int, by_name: str) -> tuple[Ticket, bool]:
        """Отмечает сканирование. Возвращает (билет, первое_ли_это_сканирование)."""
        now = now_iso()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE tickets SET scan_count = 1, first_scan_at = ?,"
                " first_scan_by_name = ?, last_scan_at = ?"
                " WHERE id = ? AND scan_count = 0",
                (now, by_name, now, ticket_id),
            )
            first = cur.rowcount == 1
            if not first:
                self.conn.execute(
                    "UPDATE tickets SET scan_count = scan_count + 1, last_scan_at = ?"
                    " WHERE id = ?",
                    (now, ticket_id),
                )
        return self.get_ticket(ticket_id), first

    def reset_scan(self, ticket_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE tickets SET scan_count = 0, first_scan_at = NULL,"
                " first_scan_by_name = NULL, last_scan_at = NULL WHERE id = ?",
                (ticket_id,),
            )

    def delete_ticket(self, ticket_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

    def reset_tickets(self) -> int:
        """Полный сброс списка клиентов. Нумерация билетов начинается заново."""
        with self.conn:
            n = self.conn.execute("DELETE FROM tickets").rowcount
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tickets'")
        return n

    def reset_orders(self) -> list[str]:
        """Удаляет все заказы (при полном сбросе). Возвращает имена файлов чеков для удаления."""
        with self.conn:
            files = [r["file"] for r in self.conn.execute("SELECT file FROM order_receipts")]
            files += [r["receipt_file"] for r in self.conn.execute(
                "SELECT receipt_file FROM orders WHERE receipt_file != ''")]
            self.conn.execute("DELETE FROM order_receipts")
            self.conn.execute("DELETE FROM orders")
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name = 'orders'")
        return files

    # ---------- типы билетов ----------

    TYPE_FIELDS = {
        "title", "description", "price", "quantity", "active", "pay_url", "pay_bin", "sort",
        "has_design", "design_box", "design_name",
    }

    def list_types(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM ticket_types" + (" WHERE active = 1" if active_only else "")
        return self.conn.execute(sql + " ORDER BY sort, id").fetchall()

    def get_type(self, type_id: int | None) -> sqlite3.Row | None:
        if type_id is None:
            return None
        return self.conn.execute(
            "SELECT * FROM ticket_types WHERE id = ?", (type_id,)
        ).fetchone()

    def add_type(
        self, title: str, description: str, price: int, quantity: int | None,
        pay_url: str, active: bool = True, pay_bin: str = "",
    ) -> int:
        with self.conn:
            top = self.conn.execute("SELECT COALESCE(MAX(sort), 0) FROM ticket_types").fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO ticket_types (title, description, price, quantity, active, pay_url,"
                " pay_bin, sort, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (title, description, price, quantity, int(active), pay_url, pay_bin, top + 1, now_iso()),
            )
        return cur.lastrowid

    def update_type(self, type_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in self.TYPE_FIELDS}
        if not fields:
            return
        with self.conn:
            self.conn.execute(
                f"UPDATE ticket_types SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), type_id),
            )

    def delete_type(self, type_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM ticket_types WHERE id = ?", (type_id,))

    def type_usage(self, type_id: int) -> int:
        """Сколько билетов и заказов связано с типом (если есть — удалять нельзя)."""
        return self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM tickets WHERE type_id = ?)"
            " + (SELECT COUNT(*) FROM orders WHERE type_id = ?)", (type_id, type_id),
        ).fetchone()[0]

    def types_overview(self) -> list[dict]:
        """Типы с продажами: sold — выдано билетов, pending — оплат на проверке (они резервируют
        места), left — сколько ещё можно продать (None — без лимита), revenue — принятые оплаты."""
        out = []
        for t in self.list_types():
            sold = self.conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE type_id = ?", (t["id"],)).fetchone()[0]
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM orders WHERE type_id = ? AND status = 'pending'",
                (t["id"],)).fetchone()[0]
            revenue = self.conn.execute(
                "SELECT COALESCE(SUM(price), 0) FROM orders WHERE type_id = ? AND status = 'approved'",
                (t["id"],)).fetchone()[0]
            left = None if t["quantity"] is None else max(0, t["quantity"] - sold - pending)
            out.append({"row": t, "sold": sold, "pending": pending, "left": left, "revenue": revenue})
        return out

    def type_left(self, type_id: int) -> int | None:
        for item in self.types_overview():
            if item["row"]["id"] == type_id:
                return item["left"]
        return 0

    # ---------- заказы (покупки через бота) ----------

    ORDER_FIELDS = {
        "status", "full_name", "phone", "university", "receipt_file", "receipt_no",
        "receipt_info", "submitted_at", "reviewed_at", "reviewed_by", "reject_reason",
        "ticket_id", "delivered", "type_id", "type_title", "price",
    }
    FLOW = ("chosen", "receipt", "name", "phone", "univ")   # клиент ещё в процессе покупки
    ACTIVE = FLOW + ("topup",)    # «topup» — внесена часть суммы: заказ ждёт доплаты и не отменяется сам

    def create_order(self, user_id: int, username: str | None, tg_name: str, t: sqlite3.Row) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders (user_id, username, tg_name, type_id, type_title, price,"
                " status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, username, tg_name, t["id"], t["title"], t["price"], "chosen", now_iso()),
            )
        return cur.lastrowid

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def active_order(self, user_id: int) -> sqlite3.Row | None:
        marks = ",".join("?" * len(self.ACTIVE))
        return self.conn.execute(
            f"SELECT * FROM orders WHERE user_id = ? AND status IN ({marks}) ORDER BY id DESC LIMIT 1",
            (user_id, *self.ACTIVE),
        ).fetchone()

    def update_order(self, order_id: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in self.ORDER_FIELDS}
        if not fields:
            return
        with self.conn:
            self.conn.execute(
                f"UPDATE orders SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), order_id),
            )

    def claim_order(self, order_id: int, from_statuses: tuple[str, ...], **fields: Any) -> bool:
        """Меняет заказ, только если он сейчас в одном из from_statuses (защита от двойного
        нажатия «принять» двумя админами). True — заказ изменён этим вызовом."""
        fields = {k: v for k, v in fields.items() if k in self.ORDER_FIELDS}
        marks = ",".join("?" * len(from_statuses))
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE orders SET {', '.join(f'{k} = ?' for k in fields)}"
                f" WHERE id = ? AND status IN ({marks})",
                (*fields.values(), order_id, *from_statuses),
            )
        return cur.rowcount == 1

    def cancel_flow_orders(self, user_id: int) -> None:
        """Клиент начал заново: незавершённые заказы отменяются."""
        marks = ",".join("?" * len(self.FLOW))
        with self.conn:
            self.conn.execute(
                f"UPDATE orders SET status = 'cancelled' WHERE user_id = ? AND status IN ({marks})",
                (user_id, *self.FLOW),
            )

    def orders_by_status(self, status: str | None, limit: int = 200) -> list[sqlite3.Row]:
        if status == "pending":   # ждущие проверки — по порядку поступления
            return self.conn.execute(
                "SELECT * FROM orders WHERE status = 'pending' ORDER BY submitted_at, id LIMIT ?",
                (limit,)).fetchall()
        if status:
            return self.conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit)).fetchall()
        return self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('topup','pending','approved','rejected')"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def orders_of_user(self, user_id: int, limit: int = 6) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE user_id = ? AND status IN ('topup','pending','approved','rejected')"
            " ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()

    def order_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def revenue(self) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(price), 0) FROM orders WHERE status = 'approved'").fetchone()[0]

    # ---------- чеки заказов ----------

    def add_receipt(self, order_id: int, file: str, number: str, amount: int | None, info: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO order_receipts (order_id, file, number, amount, info, created_at)"
                " VALUES (?,?,?,?,?,?)", (order_id, file, number, amount, info, now_iso()),
            )
        return cur.lastrowid

    def receipts_of(self, order_id: int, include_void: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM order_receipts WHERE order_id = ?" + ("" if include_void else " AND void = 0")
        return self.conn.execute(sql + " ORDER BY id", (order_id,)).fetchall()

    def get_receipt(self, receipt_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM order_receipts WHERE id = ?", (receipt_id,)).fetchone()

    def void_receipts(self, order_id: int) -> None:
        """Чеки отозваны (оплата не принята): они больше не засчитываются, но остаются в истории."""
        with self.conn:
            self.conn.execute("UPDATE order_receipts SET void = 1 WHERE order_id = ?", (order_id,))

    def paid_total(self, order_id: int) -> tuple[int, int]:
        """(сумма по чекам, число чеков, у которых сумма не распознана)."""
        rows = self.receipts_of(order_id)
        return sum(r["amount"] or 0 for r in rows if r["amount"] is not None), sum(1 for r in rows if r["amount"] is None)

    def find_receipt(self, number: str, exclude_order: int = 0) -> sqlite3.Row | None:
        """Чек с таким номером в другом заказе (ждущем проверки, принятом или ждущем доплаты)."""
        if not number:
            return None
        return self.conn.execute(
            "SELECT r.* FROM order_receipts r JOIN orders o ON o.id = r.order_id"
            " WHERE r.number = ? AND r.order_id != ? AND r.void = 0"
            " AND o.status IN ('topup','pending','approved') LIMIT 1", (number, exclude_order)).fetchone()

    def order_has_receipt(self, order_id: int, number: str) -> bool:
        return bool(number) and self.conn.execute(
            "SELECT 1 FROM order_receipts WHERE order_id = ? AND number = ? AND void = 0",
            (order_id, number)).fetchone() is not None

    # ---------- администраторы ----------

    def find_admin(self, user_id: int, username: str | None) -> sqlite3.Row | None:
        """Админ по Telegram ID, а если такого нет — по нику среди тех,
        кого добавили по нику и кто ещё ни разу не писал боту."""
        row = self.conn.execute(
            "SELECT * FROM admins WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None and username:
            row = self.conn.execute(
                "SELECT * FROM admins WHERE user_id IS NULL AND username = ?",
                (username.lower(),),
            ).fetchone()
        return row

    def get_admin(self, admin_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM admins WHERE id = ?", (admin_id,)
        ).fetchone()

    def admin_exists(self, user_id: int | None, username: str | None) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM admins WHERE (?1 IS NOT NULL AND user_id = ?1)"
                " OR (?2 IS NOT NULL AND username = ?2)",
                (user_id, username),
            ).fetchone()
            is not None
        )

    def add_admin(
        self, user_id: int | None, username: str | None, name: str, perms: str
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO admins (user_id, username, name, perms, added_at)"
                " VALUES (?,?,?,?,?)",
                (user_id, username, name, perms, now_iso()),
            )
        return cur.lastrowid

    def update_admin_profile(
        self, admin_id: int, user_id: int, username: str | None, name: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE admins SET user_id = ?, username = ?, name = ? WHERE id = ?",
                (user_id, username, name, admin_id),
            )

    def set_admin_perms(self, admin_id: int, perms: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE admins SET perms = ? WHERE id = ?", (perms, admin_id)
            )

    def remove_admin(self, admin_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))

    def list_admins(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM admins ORDER BY id").fetchall()

    # ---------- настройки ----------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def delete_setting(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
