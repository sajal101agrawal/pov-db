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
from app.etl.pipeline import Pipeline
from app.sources.rates import IndiaRiskFreeRateClient
from app.services.factory import build_bhavcopy_source, build_corporate_actions_source


async def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute IV/Greeks, daily metrics, and straddle PnL from existing DB rows.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols to recompute.")
    parser.add_argument("--skip-action-sync", action="store_true")
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    pipeline = Pipeline(
        settings=settings,
        repository=repo,
        bhavcopy_source=build_bhavcopy_source(settings),
        rates=IndiaRiskFreeRateClient(settings.default_risk_free_rate),
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]

    try:
        if not args.skip_action_sync:
            actions = await build_corporate_actions_source(settings).fetch_actions(
                start - timedelta(days=365), end, symbols
            )
            await repo.upsert_corporate_actions(actions)
        await repo.resolve_corporate_action_factors(
            start=start - timedelta(days=365), end=end, symbols=symbols
        )
        dates = [
            row["trade_date"]
            for row in await repo.pool.fetch(
                """
                SELECT DISTINCT trade_date
                FROM options_historical
                WHERE trade_date BETWEEN $1 AND $2
                  AND symbol = ANY($3::varchar[])
                ORDER BY trade_date
                """,
                start,
                end,
                symbols,
            )
        ]
        total = len(dates) * len(symbols)
        done = 0
        for date_index, trade_date in enumerate(dates, start=1):
            for symbol in symbols:
                has_rows = await repo.pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM options_historical o
                        JOIN equity_historical e USING (symbol, trade_date)
                        WHERE o.symbol = $1 AND o.trade_date = $2
                    )
                    """,
                    symbol,
                    trade_date,
                )
                if has_rows:
                    await pipeline.compute_symbol_day(symbol, trade_date)
                done += 1
            await repo.refresh_percentiles(trade_date)
            if args.progress_every and date_index % args.progress_every == 0:
                print({"event": "progress", "trade_date": trade_date.isoformat(), "done": done, "total": total}, flush=True)

        await repo.refresh_aggregates()
        print({"event": "recompute_done", "dates": len(dates), "symbols": len(symbols), "symbol_days": total})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
