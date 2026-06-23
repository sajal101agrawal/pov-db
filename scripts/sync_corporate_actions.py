from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.services.factory import build_corporate_actions_source


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync NSE corporate actions and resolve safe OHLC adjustment factors."
    )
    parser.add_argument("--start", help="Start ex-date YYYY-MM-DD. Defaults to five years ago.")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--symbols", help="Optional comma-separated symbol filter.")
    args = parser.parse_args()

    end = date.fromisoformat(args.end)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=5 * 366)
    symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    settings = get_settings()
    repo = MarketRepository(await get_pool())
    source = build_corporate_actions_source(settings)
    try:
        actions = await source.fetch_actions(start, end, symbols)
        upserted = await repo.upsert_corporate_actions(actions)
        resolution = await repo.resolve_corporate_action_factors(
            start=start, end=end, symbols=symbols
        )
        pending = await repo.pool.fetch(
            """
            SELECT symbol, ex_date, action_type, description
            FROM corporate_actions
            WHERE adjustment_status = 'PENDING_FACTOR'
              AND ex_date BETWEEN $1 AND $2
              AND ($3::text[] IS NULL OR symbol = ANY($3::text[]))
            ORDER BY ex_date, symbol
            """,
            start,
            end,
            symbols,
        )
        print(
            json.dumps(
                {
                    "event": "corporate_actions_sync_done",
                    "start": start,
                    "end": end,
                    "fetched": len(actions),
                    "upserted": upserted,
                    "factor_resolution": resolution,
                    "pending_count": len(pending),
                    "pending": [dict(row) for row in pending[:100]],
                },
                default=str,
            ),
            flush=True,
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
