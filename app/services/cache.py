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
        await self.redis.hset(key, mapping={k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in payload.items()})
        await self.redis.expire(key, ttl)


def _decode(value):
    text = value.decode() if isinstance(value, bytes) else value
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text
