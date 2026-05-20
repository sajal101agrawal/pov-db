from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.sources.rates import IndiaRiskFreeRateClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Replace non-NSE IV convention rates with NSE's fixed 10% IV rate.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--delete-yahoo-irx", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        if args.delete_yahoo_irx:
            await repo.pool.execute("DELETE FROM interest_rates WHERE source IN ('yahoo:^IRX', 'fixed:india_91d')")
        rows = await IndiaRiskFreeRateClient(settings.default_risk_free_rate).fetch_91d_rate(
            start - timedelta(days=10),
            end,
        )
        inserted = await repo.upsert_interest_rates(rows)
        print({"start": start.isoformat(), "end": end.isoformat(), "rates": inserted})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
