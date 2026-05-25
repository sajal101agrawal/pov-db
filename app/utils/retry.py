from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
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
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = min(max_delay_seconds, max(delay, retry_after))
            delay += random.uniform(0, delay * 0.1)
            await asyncio.sleep(delay)
    raise RuntimeError("retry loop exited without result") from last_exc


def _retry_after_seconds(exc: Exception) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delta = retry_at.timestamp() - datetime.now(timezone.utc).timestamp()
            return max(0.0, delta)
        except (TypeError, ValueError):
            return None
