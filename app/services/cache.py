from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis


IST = ZoneInfo("Asia/Kolkata")


def seconds_until_midnight_ist(now: datetime | None = None) -> int:
    current = now.astimezone(IST) if now else datetime.now(IST)
    tomorrow = current.date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, time.min, tzinfo=IST)
    return max(1, int((midnight - current).total_seconds()))


class CacheService:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    # ── Generic helpers ───────────────────────────────────────────────────────

    async def get_json(self, key: str) -> dict | list | None:
        raw = await self.redis.get(key)
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, payload: dict | list, ex: int | None = None) -> None:
        await self.redis.set(key, json.dumps(payload, default=str), ex=ex or seconds_until_midnight_ist())

    # ── Per-symbol dashboard ─────────────���──────────────────────────────���─────

    async def get_dashboard(self, symbol: str) -> dict | None:
        raw = await self.redis.get(f"dashboard:{symbol.upper()}")
        return json.loads(raw) if raw else None

    async def set_dashboard(self, symbol: str, payload: dict) -> None:
        await self.redis.set(f"dashboard:{symbol.upper()}", json.dumps(payload, default=str), ex=seconds_until_midnight_ist())

    async def get_live(self, symbol: str) -> dict:
        live = await self.redis.hgetall(f"live:{symbol.upper()}")
        return {k.decode() if isinstance(k, bytes) else k: _decode(v) for k, v in live.items()}

    async def set_live(self, symbol: str, payload: dict, ttl: int = 60) -> None:
        key = f"live:{symbol.upper()}"
        mapping = {k: json.dumps(v, default=str) for k, v in payload.items()}
        await self.redis.hset(key, mapping=mapping)
        await self.redis.expire(key, ttl)

    async def get_live_symbols(self) -> list[dict]:
        raw = await self.redis.get("live:symbols")
        return json.loads(raw) if raw else []

    async def set_live_symbols(self, payload: list[dict], ttl: int = 60) -> None:
        await self.redis.set("live:symbols", json.dumps(payload, default=str), ex=ttl)


def _decode(value):
    text = value.decode() if isinstance(value, bytes) else value
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text
