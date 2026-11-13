"""Публичный HTTPS-адрес для мини-приложения через бесплатный туннель Cloudflare.

Telegram открывает мини-приложения только по HTTPS, а бот обычно работает на
домашнем компьютере без своего домена. Программа cloudflared даёт временный
адрес вида https://….trycloudflare.com, который ведёт на локальный веб-сервер
бота. Регистрация не нужна; адрес меняется при каждом запуске.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import re
import shutil
import sys
from pathlib import Path

import aiohttp

from .config import BASE_DIR

log = logging.getLogger(__name__)

RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
START_TIMEOUT = 45


def _asset() -> str | None:
    """Имя файла cloudflared для этой системы в официальных релизах."""
    arm = platform.machine().lower() in {"arm64", "aarch64"}
    if sys.platform == "win32":
        return "cloudflared-windows-amd64.exe"
    if sys.platform.startswith("linux"):
        return "cloudflared-linux-arm64" if arm else "cloudflared-linux-amd64"
    return None  # для macOS официальный файл — архив; ставится через brew


async def find_cloudflared(data_dir: Path) -> Path | None:
    """Ищет cloudflared в системе и в папке проекта; если нет — скачивает
    официальную сборку с GitHub в папку данных."""
    exe = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    found = shutil.which("cloudflared")
    for candidate in (found, BASE_DIR / exe, data_dir / exe):
        if candidate and Path(candidate).exists():
            return Path(candidate)

    asset = _asset()
    if not asset:
        log.warning("cloudflared не найден. Установите его (brew install cloudflared).")
        return None
    target = data_dir / exe
    log.info("Скачиваю cloudflared (официальная сборка Cloudflare) в %s ...", target)
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(RELEASES + asset) as resp:
                resp.raise_for_status()
                part = target.with_suffix(".part")
                with open(part, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        f.write(chunk)
        part.replace(target)
        target.chmod(0o755)
        return target
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        log.warning("Не удалось скачать cloudflared: %s", e)
        return None


class Tunnel:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None

    async def start(self, port: int, data_dir: Path) -> str | None:
        """Запускает туннель на локальный порт. Возвращает публичный адрес или None."""
        binary = await find_cloudflared(data_dir)
        if not binary:
            return None
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(binary), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, NotImplementedError) as e:
            log.warning("Не удалось запустить cloudflared: %s", e)
            return None

        found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def read() -> None:
            # Читаем вывод всё время работы, иначе cloudflared упрётся в полный буфер.
            assert self.process and self.process.stderr
            async for line in self.process.stderr:
                m = URL_RE.search(line.decode(errors="replace"))
                if m and not found.done():
                    found.set_result(m.group(0))
            if not found.done():
                found.set_exception(RuntimeError("cloudflared завершился"))

        self._reader = asyncio.create_task(read())
        try:
            return await asyncio.wait_for(found, START_TIMEOUT)
        except (asyncio.TimeoutError, RuntimeError) as e:
            log.warning("Туннель не поднялся: %s", e or "нет ответа")
            await self.stop()
            return None

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
        if self._reader:
            self._reader.cancel()
