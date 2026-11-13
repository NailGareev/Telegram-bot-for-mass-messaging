"""Уведомления для открытого Mini App.

Приложение держит «длинный опрос» (events/poll): пока оно открыто, запрос почти
всё время висит на сервере. Если админ в этот момент сканирует билет в чате
(ссылка из QR, фото, номер), результат уходит в приложение, а не в чат. Если
приложение не подтвердило получение за ACK_WAIT секунд — результат всё равно
приходит в чат, так что уведомление не теряется.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

EVENT_TTL = 120     # сколько секунд событие ждёт, пока приложение его заберёт
OPEN_GAP = 6        # пауза между опросами, в течение которой приложение считается открытым
ACK_WAIT = 10       # через сколько секунд без подтверждения отправить запасной вариант в чат


class Hub:
    def __init__(self) -> None:
        self._seq = 0
        self._events: dict[int, list[tuple[float, dict]]] = defaultdict(list)
        self._waiters: dict[int, set[asyncio.Event]] = defaultdict(set)
        self._active: dict[int, int] = defaultdict(int)
        self._last_end: dict[int, float] = {}
        self._acked: dict[int, int] = defaultdict(int)
        self._tasks: set[asyncio.Task] = set()

    def is_open(self, user_id: int) -> bool:
        if self._active[user_id] > 0:
            return True
        return time.monotonic() - self._last_end.get(user_id, -1e9) < OPEN_GAP

    def publish(
        self, user_id: int, event: dict,
        fallback: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Отправляет событие открытому приложению. False — приложение закрыто,
        вызывающий должен показать уведомление иначе (например, в чате)."""
        if not self.is_open(user_id):
            return False
        self._seq += 1
        event = {**event, "id": self._seq}
        now = time.monotonic()
        buf = self._events[user_id]
        buf[:] = [e for e in buf if now - e[0] < EVENT_TTL]
        buf.append((now, event))
        for waiter in self._waiters[user_id]:
            waiter.set()
        if fallback:
            task = asyncio.get_running_loop().create_task(
                self._watch(user_id, event["id"], fallback)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return True

    async def _watch(self, user_id: int, event_id: int, fallback) -> None:
        await asyncio.sleep(ACK_WAIT)
        if self._acked[user_id] < event_id:
            try:
                await fallback()
            except Exception:
                log.exception("Не удалось отправить запасное уведомление")

    async def poll(self, user_id: int, after: int | None, wait: float) -> tuple[list[dict], int]:
        """Возвращает (новые события, номер последнего). Первый запрос приложения
        (after=None) только запоминает «сейчас», старые события не повторяются."""
        self._active[user_id] += 1
        try:
            if after is None:
                return [], self._seq
            self._acked[user_id] = max(self._acked[user_id], after)

            def pending() -> list[dict]:
                return [e for _, e in self._events[user_id] if e["id"] > after]

            events = pending()
            if not events:
                waiter = asyncio.Event()
                self._waiters[user_id].add(waiter)
                try:
                    await asyncio.wait_for(waiter.wait(), wait)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._waiters[user_id].discard(waiter)
                events = pending()
            return events, max((e["id"] for e in events), default=after)
        finally:
            self._active[user_id] -= 1
            self._last_end[user_id] = time.monotonic()
