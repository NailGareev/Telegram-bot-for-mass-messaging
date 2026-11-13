"""Настройки бота: читаются из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = BASE_DIR / "assets" / "default_ticket.png"
# Квадрат под QR на стандартном шаблоне: (x, y, сторона) в пикселях.
DEFAULT_BOX = (1230, 177, 250)


@dataclass(frozen=True)
class Config:
    bot_token: str
    main_admin_ids: frozenset[int]
    groq_api_key: str
    groq_model: str
    tz: ZoneInfo
    data_dir: Path
    ticket_prefix: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tickets.sqlite3"

    @property
    def template_path(self) -> Path:
        return self.data_dir / "template.png"

    @property
    def pending_path(self) -> Path:
        return self.data_dir / "template_pending.png"

    def is_main(self, user_id: int) -> bool:
        return user_id in self.main_admin_ids


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")

    raw_ids = os.getenv("MAIN_ADMIN_IDS", "").replace(";", ",")
    try:
        main_ids = frozenset(int(p) for p in raw_ids.split(",") if p.strip())
    except ValueError:
        raise SystemExit("MAIN_ADMIN_IDS должен содержать числовые Telegram ID через запятую")
    if not main_ids:
        raise SystemExit("Не задан MAIN_ADMIN_IDS — Telegram ID главного администратора")

    data_dir = Path(os.getenv("DATA_DIR", "") or BASE_DIR / "data")
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        bot_token=token,
        main_admin_ids=main_ids,
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "").strip()
        or "meta-llama/llama-4-scout-17b-16e-instruct",
        tz=ZoneInfo(os.getenv("TIMEZONE", "").strip() or "Asia/Almaty"),
        data_dir=data_dir,
        ticket_prefix=os.getenv("TICKET_PREFIX", "").strip() or "MV",
    )
