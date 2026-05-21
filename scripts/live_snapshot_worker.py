from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from redis.asyncio import Redis

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.services.live import live_worker_loop


async def main() -> None:
    settings = get_settings()
    repo = MarketRepository(await get_pool())
    redis = Redis.from_url(settings.redis_url)
    try:
        await live_worker_loop(settings, repo, redis)
    finally:
        await redis.aclose()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
