"""Запуск бота одной командой:  python run.py

При первом запуске сам создаёт виртуальное окружение (.venv), ставит зависимости
и создаёт файл .env, который нужно заполнить. Дальше просто запускает бота.
"""
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"
INSTALLED_MARK = VENV / ".requirements-installed"
ENV_FILE = ROOT / ".env"
REQUIRED = ("BOT_TOKEN", "MAIN_ADMIN_IDS")


def stop(message: str) -> None:
    print("\n" + message)
    if sys.platform == "win32" and sys.stdin.isatty():
        input("\nНажмите Enter, чтобы закрыть окно...")
    sys.exit(1)


def prepare_env_file() -> None:
    if not ENV_FILE.exists():
        shutil.copy(ROOT / ".env.example", ENV_FILE)
        stop(
            f"Создан файл настроек: {ENV_FILE}\n"
            "Откройте его, впишите BOT_TOKEN и MAIN_ADMIN_IDS и запустите run.py ещё раз.\n"
            "Ключ Groq и адрес сканера задаются потом в самом боте: раздел «Настройки»."
        )
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        stop(f"В файле {ENV_FILE} не заполнено: {', '.join(missing)}")


def prepare_venv() -> None:
    if not PYTHON.exists():
        print("Создаю виртуальное окружение .venv ...")
        venv.create(VENV, with_pip=True)
    if (
        not INSTALLED_MARK.exists()
        or INSTALLED_MARK.stat().st_mtime < REQUIREMENTS.stat().st_mtime
    ):
        print("Устанавливаю зависимости ...")
        result = subprocess.run(
            [str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check",
             "-r", str(REQUIREMENTS)]
        )
        if result.returncode != 0:
            stop("Не удалось установить зависимости (см. ошибку выше).")
        INSTALLED_MARK.touch()


def main() -> None:
    if sys.version_info < (3, 10):
        stop("Нужен Python 3.10 или новее.")
    prepare_env_file()
    prepare_venv()
    print("Запускаю бота (остановить — Ctrl+C) ...")
    try:
        code = subprocess.run([str(PYTHON), "-m", "bot"], cwd=ROOT).returncode
    except KeyboardInterrupt:
        code = 0
    if code != 0:
        stop("Бот остановился с ошибкой (см. сообщение выше).")


if __name__ == "__main__":
    main()
