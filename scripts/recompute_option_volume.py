from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recompute symbol_daily_metrics.avg_option_volume from all CE/PE contracts."
    )
    parser.add_argument("--start", help="Optional start date YYYY-MM-DD.")
    parser.add_argument("--end", help="Optional end date YYYY-MM-DD.")
    parser.add_argument("--symbols", help="Optional comma-separated symbols.")
    args = parser.parse_args()

    filters = ["oh.num_contracts IS NOT NULL", "oh.option_type IN ('CE', 'PE')"]
    params: list[object] = []
    if args.start:
        params.append(date.fromisoformat(args.start))
        filters.append(f"oh.trade_date >= ${len(params)}")
    if args.end:
        params.append(date.fromisoformat(args.end))
        filters.append(f"oh.trade_date <= ${len(params)}")
    if args.symbols:
        symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
        params.append(symbols)
        filters.append(f"oh.symbol = ANY(${len(params)}::text[])")

    where_clause = " AND ".join(filters)
    repo = MarketRepository(await get_pool())
    try:
        result = await repo.pool.execute(
            f"""
            WITH option_volume AS (
                SELECT oh.symbol, oh.trade_date, SUM(oh.num_contracts)::numeric AS total_contracts
                FROM options_historical oh
                WHERE {where_clause}
                GROUP BY oh.symbol, oh.trade_date
            )
            UPDATE symbol_daily_metrics sdm
            SET avg_option_volume = option_volume.total_contracts,
                updated_at = NOW()
            FROM option_volume
            WHERE sdm.symbol = option_volume.symbol
              AND sdm.trade_date = option_volume.trade_date
            """,
            *params,
        )
        print({"event": "option_volume_recomputed", "updated": int(result.split()[-1])}, flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
