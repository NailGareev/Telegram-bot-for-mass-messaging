"""Кто есть кто: главный администратор, админы и их разрешения по разделам."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject, User

from .config import Config
from .db import Database

# Разделы бота: ключ -> название. Порядок — как в меню.
PERMS = {
    "new": "Новый билет",
    "scan": "Сканировать",
    "list": "Список и поиск (все билеты)",
    "stats": "Статистика",
    "admins": "Админы",
    "design": "Дизайн билета",
    "reset": "Сброс списка",
}
DEFAULT_PERMS = "new,scan"


def parse_perms(raw: str) -> frozenset[str]:
    return frozenset(p for p in raw.split(",") if p in PERMS)


def dump_perms(perms) -> str:
    return ",".join(p for p in PERMS if p in perms)


@dataclass(frozen=True)
class Access:
    user_id: int | None
    is_main: bool
    perms: frozenset[str]

    def can(self, perm: str) -> bool:
        return self.is_main or perm in self.perms

    @property
    def tickets_owner(self) -> int | None:
        """Чьи билеты видны в списке и поиске: None — все, иначе только свои."""
        return None if self.can("list") else self.user_id


def resolve_access(user: User, db: Database, config: Config) -> Access | None:
    """Права пользователя или None, если это посторонний."""
    if config.is_main(user.id):
        return Access(user.id, True, frozenset(PERMS))
    row = db.find_admin(user.id, user.username)
    if row is None:
        return None
    username = user.username.lower() if user.username else None
    if (row["user_id"], row["username"], row["name"]) != (user.id, username, user.full_name):
        # первый вход добавленного по нику либо смена имени/ника
        db.update_admin_profile(row["id"], user.id, username, user.full_name)
    return Access(user.id, False, parse_perms(row["perms"]))


class Perm(BaseFilter):
    """Пропускает только тех, у кого есть разрешение на раздел."""

    def __init__(self, perm: str):
        self.perm = perm

    async def __call__(self, event: TelegramObject, access: Access) -> bool:
        return access.can(self.perm)
