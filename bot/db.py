"""Лёгкая база на SQLite: билеты, администраторы, настройки."""
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
    last_scan_at       TEXT
);
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

    # ---------- билеты ----------

    def create_ticket(
        self,
        full_name: str,
        study_group: str,
        university: str,
        phone: str,
        created_by: int,
        created_by_name: str,
    ) -> Ticket:
        token = secrets.token_urlsafe(12)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tickets (token, full_name, study_group, university, phone,"
                " created_at, created_by, created_by_name) VALUES (?,?,?,?,?,?,?,?)",
                (token, full_name, study_group, university, phone, now_iso(),
                 created_by, created_by_name),
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
