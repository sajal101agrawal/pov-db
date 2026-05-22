from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import _weekly_closes
from app.services.calculations import rsi


async def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute daily/weekly RSI in symbol_daily_metrics.")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--symbols")
    parser.add_argument("--progress-every", type=int, default=500)
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    symbols = [item.strip().upper() for item in args.symbols.split(",")] if args.symbols else None

    repo = MarketRepository(await get_pool())
    filters = []
    params: list[object] = []
    if start:
        params.append(start)
        filters.append(f"trade_date >= ${len(params)}")
    if end:
        params.append(end)
        filters.append(f"trade_date <= ${len(params)}")
    if symbols:
        params.append(symbols)
        filters.append(f"symbol = ANY(${len(params)}::text[])")
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = await repo.pool.fetch(
        f"""
        SELECT symbol, trade_date
        FROM symbol_daily_metrics
        {where}
        ORDER BY trade_date, symbol
        """,
        *params,
    )

    updated = 0
    try:
        for idx, row in enumerate(rows, start=1):
            symbol = row["symbol"]
            trade_date = row["trade_date"]
            ohlc = await repo.equity_ohlc_window(symbol, trade_date, limit=500)
            closes = [item["close"] for item in ohlc if item.get("close")]
            daily_rsi = rsi(closes, 14)
            weekly_rsi = rsi(_weekly_closes(ohlc), 14)
            await repo.pool.execute(
                """
                UPDATE symbol_daily_metrics
                SET daily_rsi = $3,
                    weekly_rsi = $4,
                    updated_at = NOW()
                WHERE symbol = $1 AND trade_date = $2
                """,
                symbol,
                trade_date,
                daily_rsi,
                weekly_rsi,
            )
            updated += 1
            if args.progress_every and idx % args.progress_every == 0:
                print({"event": "progress", "done": idx, "total": len(rows), "updated": updated}, flush=True)
        print({"event": "rsi_recompute_done", "updated": updated}, flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
