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
from app.services.corporate_actions import calculate_price_series_metrics
from app.services.factory import build_corporate_actions_source


async def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute daily/weekly RSI in symbol_daily_metrics.")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--symbols")
    parser.add_argument("--skip-action-sync", action="store_true")
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
        if rows and not args.skip_action_sync:
            action_start = (start or rows[0]["trade_date"]) - timedelta(days=365)
            action_end = end or rows[-1]["trade_date"]
            settings = get_settings()
            actions = await build_corporate_actions_source(settings).fetch_actions(
                action_start, action_end, symbols
            )
            await repo.upsert_corporate_actions(actions)
            await repo.resolve_corporate_action_factors(
                start=action_start, end=action_end, symbols=symbols
            )
        for idx, row in enumerate(rows, start=1):
            symbol = row["symbol"]
            trade_date = row["trade_date"]
            ohlc = await repo.equity_ohlc_window(symbol, trade_date, limit=100)
            if not ohlc:
                continue
            actions = await repo.corporate_actions_window(
                symbol, ohlc[0]["trade_date"], trade_date
            )
            price_metrics = calculate_price_series_metrics(ohlc, actions, trade_date)
            daily_rsi = price_metrics["daily_rsi"]
            weekly_rsi = price_metrics["weekly_rsi"]
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
