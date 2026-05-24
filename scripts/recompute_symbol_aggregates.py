from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository


async def main() -> None:
    repo = MarketRepository(await get_pool())
    try:
        await repo.refresh_aggregates()
        count = await repo.pool.fetchval("SELECT COUNT(*) FROM symbol_aggregates")
        print({"event": "symbol_aggregates_recomputed", "rows": int(count or 0)}, flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
