from __future__ import annotations

import asyncio
import json
from datetime import date

from app.services.cache import CacheService


def test_set_live_encodes_none_and_dates_for_redis_hashes() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.mapping = {}
            self.ttl = None

        async def hset(self, key: str, mapping: dict) -> None:
            self.key = key
            self.mapping = mapping

        async def expire(self, key: str, ttl: int) -> None:
            self.expire_key = key
            self.ttl = ttl

    redis = FakeRedis()

    asyncio.run(
        CacheService(redis).set_live(
            "RELIANCE",
            {"current_price": 1420.5, "iv_30": None, "trade_date": date(2026, 5, 25)},
            ttl=300,
        )
    )

    assert redis.key == "live:RELIANCE"
    assert redis.ttl == 300
    assert json.loads(redis.mapping["current_price"]) == 1420.5
    assert json.loads(redis.mapping["iv_30"]) is None
    assert json.loads(redis.mapping["trade_date"]) == "2026-05-25"
