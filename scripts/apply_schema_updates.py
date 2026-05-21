from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool


STATEMENTS = [
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_60 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_90 NUMERIC(18,8)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS avg_earnings_pnl NUMERIC(12,4)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS earnings_win_rate NUMERIC(6,2)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS max_earnings_profit NUMERIC(12,4)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS max_earnings_loss NUMERIC(12,4)",
]


async def main() -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            for statement in STATEMENTS:
                await conn.execute(statement)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
