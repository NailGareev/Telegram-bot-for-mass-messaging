"""Обновление проекта из zip-архива GitHub, присланного главным администратором в чат.

Порядок работы (каждый шаг безопасен — при ошибке бот остаётся на прежней версии):

1. prepare(): архив проверяется и распаковывается в папку-черновик; считается,
   что изменится. Ничего в рабочей папке не трогается.
2. apply(): ставятся новые зависимости, новая версия проверяется пробным запуском
   (import), текущая версия сохраняется в архив-копию, файлы заменяются.
3. После перезапуска бот подтверждает успешный старт. Если новая версия не смогла
   стартовать три раза подряд, прежняя возвращается автоматически (startup_check).

Не затрагиваются: .env, папка data/ (база, дизайн билета, копии), .venv, .tools, .git.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

RESTART_CODE = 75          # код выхода «перезапустите меня»: служба systemd / run.py сделают это сами
PROTECTED = {".env", "data", ".venv", ".tools", ".git", ".idea", ".vscode", "__pycache__"}
MAX_ENTRIES = 3000
MAX_TOTAL_BYTES = 60 * 1024 * 1024
KEEP_BACKUPS = 5
ROLLBACK_AFTER_STARTS = 3  # столько запусков подряд без подтверждения — и возвращаем прежнюю версию


class UpdateError(Exception):
    """Сообщение показывается главному администратору."""


class Restarter:
    """Просьба обработчика перезапустить бота после обновления."""

    def __init__(self) -> None:
        self.requested = False
        self.event = asyncio.Event()

    def request(self) -> None:
        self.requested = True
        self.event.set()


def supervised() -> bool:
    """Перезапустит ли бота кто-то снаружи (systemd или run.py)."""
    return bool(os.getenv("INVOCATION_ID") or os.getenv("TICKET_BOT_SUPERVISED"))


@dataclass
class Plan:
    source: str = ""
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    requirements_changed: bool = False

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def _dir(data_dir: Path) -> Path:
    return data_dir / "update"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skip(parts: tuple[str, ...], rel: str) -> bool:
    return parts[0] in PROTECTED or "__pycache__" in parts or rel.endswith((".pyc", ".pyo"))


# ---------- 1. проверка и подготовка ----------


def prepare(raw: bytes, base_dir: Path, data_dir: Path, source: str = "") -> Plan:
    upd = _dir(data_dir)
    staged = upd / "staged"
    shutil.rmtree(staged, ignore_errors=True)
    (upd / "plan.json").unlink(missing_ok=True)
    staged.mkdir(parents=True)

    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise UpdateError("Это не zip-архив или он повреждён.")
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise UpdateError("Архив слишком большой для проекта.")

    names = {i.filename for i in infos}
    roots = sorted((n[: -len("bot/main.py")] for n in names if n.endswith("bot/main.py")), key=len)
    prefix = None
    for candidate in roots:
        if candidate.count("/") <= 1 and {candidate + "bot/__init__.py", candidate + "requirements.txt"} <= names:
            prefix = candidate
            break
    if prefix is None:
        raise UpdateError(
            "В архиве не найден проект бота (нужны папка bot/ и файл requirements.txt). "
            "Скачайте архив на GitHub: Code → Download ZIP."
        )

    for info in infos:
        if info.is_dir() or not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix):]
        parts = PurePosixPath(rel).parts
        if (
            not parts or "\\" in rel or rel.startswith("/") or ".." in parts
            or any(":" in p for p in parts[:1])
        ):
            raise UpdateError("В архиве есть небезопасные пути — обновление отменено.")
        if _skip(parts, rel):
            continue
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            continue
        dest = staged.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(z.read(info))
        os.chmod(dest, 0o755 if mode & 0o111 else 0o644)

    files = {p.relative_to(staged).as_posix(): p for p in staged.rglob("*") if p.is_file()}
    plan = Plan(source=source)
    for rel, path in sorted(files.items()):
        current = base_dir / rel
        if not current.is_file():
            plan.added.append(rel)
        elif _sha(current) != _sha(path):
            plan.changed.append(rel)
    # Папки проекта заменяются целиком, поэтому файлы, которых больше нет в новой версии, удалятся.
    for top in sorted({rel.split("/")[0] for rel in files if "/" in rel}):
        for path in (base_dir / top).rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base_dir).as_posix()
            if rel not in files and not _skip(PurePosixPath(rel).parts, rel):
                plan.removed.append(rel)
    req_new, req_old = staged / "requirements.txt", base_dir / "requirements.txt"
    plan.requirements_changed = req_new.is_file() and (
        not req_old.is_file() or _sha(req_new) != _sha(req_old)
    )
    if plan.empty:
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError("Обновлений нет: в этом архиве та же версия, что уже установлена.")
    (upd / "plan.json").write_text(json.dumps(asdict(plan), ensure_ascii=False), encoding="utf-8")
    return plan


def load_plan(data_dir: Path) -> Plan:
    path = _dir(data_dir) / "plan.json"
    if not path.is_file() or not (_dir(data_dir) / "staged").is_dir():
        raise UpdateError("Архив не найден — пришлите его в чат заново.")
    return Plan(**json.loads(path.read_text(encoding="utf-8")))


def discard(data_dir: Path) -> None:
    shutil.rmtree(_dir(data_dir) / "staged", ignore_errors=True)
    (_dir(data_dir) / "plan.json").unlink(missing_ok=True)


# ---------- 2. установка ----------


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-700:]


def _pip_install(requirements: Path) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0 and "No module named pip" in result.stderr:
        # Окружение, созданное через uv, может быть без pip.
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], capture_output=True, timeout=300)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise UpdateError("Не удалось установить зависимости:\n" + _tail(result.stderr or result.stdout))


def _preflight(staged: Path) -> None:
    """Пробный запуск: новая версия должна хотя бы импортироваться."""
    code = "import sys; sys.path.insert(0, sys.argv[1]); import bot.main"
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(staged)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise UpdateError("Новая версия не запускается:\n" + _tail(result.stderr))


def _backup(base_dir: Path, data_dir: Path, tops: list[str]) -> Path:
    folder = data_dir / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / time.strftime("before-%Y%m%d-%H%M%S.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for top in tops:
            path = base_dir / top
            items = [path] if path.is_file() else sorted(path.rglob("*")) if path.is_dir() else []
            for item in items:
                rel = item.relative_to(base_dir).as_posix()
                if item.is_file() and not _skip(PurePosixPath(rel).parts, rel):
                    info = zipfile.ZipInfo.from_file(item, rel)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, item.read_bytes())
    for old in sorted(folder.glob("before-*.zip"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _restore(backup: Path, base_dir: Path) -> None:
    with zipfile.ZipFile(backup) as z:
        tops = {PurePosixPath(i.filename).parts[0] for i in z.infolist() if not i.is_dir()}
        for top in tops:
            if (base_dir / top).is_dir():
                shutil.rmtree(base_dir / top, ignore_errors=True)
        for info in z.infolist():
            if info.is_dir():
                continue
            dest = base_dir.joinpath(*PurePosixPath(info.filename).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(info))
            mode = info.external_attr >> 16
            os.chmod(dest, 0o755 if mode & 0o111 else 0o644)


def apply(base_dir: Path, data_dir: Path, meta: dict) -> Plan:
    """Устанавливает подготовленное обновление. Блокирующая — вызывать через to_thread."""
    plan = load_plan(data_dir)
    staged = _dir(data_dir) / "staged"
    if plan.requirements_changed:
        _pip_install(staged / "requirements.txt")
    _preflight(staged)

    files = [p for p in staged.rglob("*") if p.is_file()]
    tops = sorted({p.relative_to(staged).parts[0] for p in files})
    backup = _backup(base_dir, data_dir, tops)
    try:
        for top in tops:
            src, dst = staged / top, base_dir / top
            if src.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
            else:
                shutil.copyfile(src, dst)
                os.chmod(dst, src.stat().st_mode & 0o777)
    except Exception as e:
        log.exception("Ошибка при замене файлов, возвращаю прежнюю версию")
        _restore(backup, base_dir)
        raise UpdateError(f"Не удалось заменить файлы ({e.__class__.__name__}). Прежняя версия восстановлена.")

    (_dir(data_dir) / "pending.json").write_text(
        json.dumps({
            "backup": str(backup), "attempts": 0, "time": int(time.time()),
            "added": len(plan.added), "changed": len(plan.changed), "removed": len(plan.removed),
            **meta,
        }),
        encoding="utf-8",
    )
    discard(data_dir)
    return plan


# ---------- 3. проверка запуска ----------


def startup_check(base_dir: Path, data_dir: Path) -> bool:
    """Вызывается при каждом запуске. True — прежняя версия возвращена, нужен перезапуск."""
    marker = _dir(data_dir) / "pending.json"
    if not marker.is_file():
        return False
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        info["attempts"] = int(info.get("attempts", 0)) + 1
        marker.write_text(json.dumps(info), encoding="utf-8")
        if info["attempts"] < ROLLBACK_AFTER_STARTS:
            return False
        log.warning("Новая версия не запустилась %s раза подряд — возвращаю прежнюю", info["attempts"])
        _restore(Path(info["backup"]), base_dir)
    except Exception:
        log.exception("Не удалось вернуть прежнюю версию")
        return False
    finally:
        pass
    marker.unlink(missing_ok=True)
    return True


async def confirm_started(bot, data_dir: Path) -> None:
    """Новая версия стартовала: снимаем отметку и сообщаем администратору."""
    marker = _dir(data_dir) / "pending.json"
    if not marker.is_file():
        return
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except ValueError:
        info = {}
    marker.unlink(missing_ok=True)
    chat_id = info.get("chat_id")
    if chat_id:
        try:
            await bot.send_message(
                chat_id,
                "✅ <b>Проект обновлён</b>, бот перезапущен и работает.\n"
                f"Новых файлов: {info.get('added', 0)} · изменено: {info.get('changed', 0)} · "
                f"удалено: {info.get('removed', 0)}",
            )
        except Exception:
            log.exception("Не удалось отправить подтверждение обновления")
