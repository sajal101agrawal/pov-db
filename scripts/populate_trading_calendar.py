from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository


async def main() -> None:
    parser = argparse.ArgumentParser(description="Populate trading_calendar from locally loaded NSE market data.")
    parser.add_argument("--start", help="Start date YYYY-MM-DD. Defaults to first equity date.")
    parser.add_argument("--end", help="End date YYYY-MM-DD. Defaults to latest equity date.")
    args = parser.parse_args()

    repo = MarketRepository(await get_pool())
    try:
        bounds = await repo.pool.fetchrow(
            """
            SELECT MIN(trade_date) AS min_date, MAX(trade_date) AS max_date
            FROM (
                SELECT trade_date FROM equity_historical
                UNION
                SELECT trade_date FROM options_historical
            ) dates
            """
        )
        if not bounds or bounds["min_date"] is None or bounds["max_date"] is None:
            print({"rows": 0, "reason": "equity_historical is empty"})
            return
        start = date.fromisoformat(args.start) if args.start else bounds["min_date"]
        end = date.fromisoformat(args.end) if args.end else bounds["max_date"]
        trading_rows = await repo.pool.fetch(
            """
            SELECT trade_date,
                   BOOL_OR(source_table = 'equity') AS has_equity,
                   BOOL_OR(source_table = 'options') AS has_options
            FROM (
                SELECT trade_date, 'equity' AS source_table
                FROM equity_historical
                WHERE trade_date BETWEEN $1 AND $2
                UNION ALL
                SELECT trade_date, 'options' AS source_table
                FROM options_historical
                WHERE trade_date BETWEEN $1 AND $2
            ) x
            GROUP BY trade_date
            """,
            start,
            end,
        )
        trading_dates = {row["trade_date"]: dict(row) for row in trading_rows}

        rows = []
        current = start
        while current <= end:
            if current in trading_dates:
                source = (
                    "equity_options_historical"
                    if trading_dates[current]["has_equity"] and trading_dates[current]["has_options"]
                    else "partial_local_bhavcopy"
                )
                rows.append({"trade_date": current, "is_trading_day": True, "source": source})
            elif current.weekday() >= 5:
                rows.append({"trade_date": current, "is_trading_day": False, "source": "weekend"})
            else:
                rows.append({"trade_date": current, "is_trading_day": False, "source": "no_local_bhavcopy"})
            current += timedelta(days=1)

        inserted = await repo.upsert_trading_calendar(rows)
        partial_days = sum(1 for row in trading_dates.values() if not (row["has_equity"] and row["has_options"]))
        print(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "rows": inserted,
                "trading_days": len(trading_dates),
                "partial_trading_days": partial_days,
            }
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
