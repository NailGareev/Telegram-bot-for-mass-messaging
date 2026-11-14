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
# Где на стандартном шаблоне печатается ФИ гостя: полоса между подзаголовком и фото.
DEFAULT_NAME = {"enabled": True, "box": [50, 164, 600, 36], "align": "left", "color": "#fdf3b8"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    main_admin_ids: frozenset[int]
    groq_api_key: str
    groq_model: str
    tz: ZoneInfo
    data_dir: Path
    ticket_prefix: str
    # Мини-приложение: включено ли, публичный HTTPS-адрес (если есть свой),
    # локальный порт веб-сервера и разрешён ли автоматический туннель.
    miniapp: bool = True
    webapp_url: str = ""
    webapp_port: int = 8080
    tunnel: bool = True
    log_level: str = "WARNING"
    groq_text_model: str = "llama-3.3-70b-versatile"

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

    webapp_url = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    if webapp_url and not webapp_url.startswith("https://"):
        raise SystemExit("WEBAPP_URL должен начинаться с https://")
    try:
        webapp_port = int(os.getenv("WEBAPP_PORT", "").strip() or 8080)
    except ValueError:
        raise SystemExit("WEBAPP_PORT должен быть числом")

    def flag(name: str) -> bool:
        return os.getenv(name, "").strip().lower() not in {"off", "0", "no", "false"}

    log_level = os.getenv("LOG_LEVEL", "").strip().upper() or "WARNING"
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise SystemExit("LOG_LEVEL: допустимы DEBUG, INFO, WARNING, ERROR")

    return Config(
        log_level=log_level,
        miniapp=flag("MINI_APP"),
        webapp_url=webapp_url,
        webapp_port=webapp_port,
        tunnel=flag("TUNNEL"),
        bot_token=token,
        main_admin_ids=main_ids,
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "").strip()
        or "meta-llama/llama-4-maverick-17b-128e-instruct",
        groq_text_model=os.getenv("GROQ_TEXT_MODEL", "").strip() or "llama-3.3-70b-versatile",
        tz=ZoneInfo(os.getenv("TIMEZONE", "").strip() or "Asia/Almaty"),
        data_dir=data_dir,
        ticket_prefix=os.getenv("TICKET_PREFIX", "").strip() or "MV",
    )
