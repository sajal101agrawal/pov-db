from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx


T = TypeVar("T")

RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
    httpx.HTTPStatusError,
)


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.75,
    max_delay_seconds: float = 8.0,
    retryable: Callable[[Exception], bool] = is_retryable_exception,
) -> T:
    last_exc: Exception | None = None
    safe_attempts = max(attempts, 1)
    for attempt in range(1, safe_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            last_exc = exc
            if attempt >= safe_attempts or not retryable(exc):
                raise
            delay = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            await asyncio.sleep(delay)
    raise RuntimeError("retry loop exited without result") from last_exc
