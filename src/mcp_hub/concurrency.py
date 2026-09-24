from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Literal, TypeVar

T = TypeVar("T")


class ConcurrencyGuard:
    def __init__(self, mode: Literal["exclusive", "parallel"]):
        self.mode = mode
        self._lock = asyncio.Lock() if mode == "exclusive" else None

    async def run(self, coro_fn: Callable[[], Awaitable[T]]) -> T:
        if self._lock is None:
            return await coro_fn()
        async with self._lock:
            return await coro_fn()
