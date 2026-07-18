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
from app.etl.pipeline import Pipeline
from app.services.factory import build_bhavcopy_source
from app.sources.rates import IndiaRiskFreeRateClient
from app.sources.yahoo import YahooFinanceClient


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Yahoo OHLC and analytics for OPTIDX symbols already present in options_historical."
    )
    parser.add_argument("--start", help="First date to backfill, YYYY-MM-DD. Defaults to earliest index option date.")
    parser.add_argument("--end", help="Last date to backfill, YYYY-MM-DD. Defaults to latest index option date.")
    parser.add_argument("--symbols", help="Optional comma-separated index symbols.")
    parser.add_argument("--execute", action="store_true", help="Write Yahoo OHLC and recomputed metrics.")
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    requested_symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    pool = await get_pool()
    repo = MarketRepository(pool)
    try:
        bounds = await pool.fetch(
            """
            SELECT symbol, MIN(trade_date) AS first_trade_date, MAX(trade_date) AS last_trade_date
            FROM options_historical
            WHERE instrument_type = 'OPTIDX'
              AND ($1::text[] IS NULL OR symbol = ANY($1::text[]))
            GROUP BY symbol
            ORDER BY symbol
            """,
            requested_symbols,
        )
        if not bounds:
            raise RuntimeError("No OPTIDX option history was found for the requested symbols.")

        start = date.fromisoformat(args.start) if args.start else min(
            row["first_trade_date"] for row in bounds
        )
        end = date.fromisoformat(args.end) if args.end else max(
            row["last_trade_date"] for row in bounds
        )
        symbols = [row["symbol"] for row in bounds]
        preview = {
            "event": "index_history_backfill_preview",
            "start": start,
            "end": end,
            "symbols": symbols,
            "execute": args.execute,
        }
        print(json.dumps(preview, default=str), flush=True)
        if not args.execute:
            print("Read-only preview complete. Re-run with --execute to apply changes.", flush=True)
            return

        client = YahooFinanceClient()
        results = await asyncio.gather(
            *[
                client.fetch_equity_history(symbol, start, end + timedelta(days=1))
                for symbol in symbols
            ],
            return_exceptions=True,
        )
        bars = []
        errors = []
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, Exception):
                errors.append(
                    {"symbol": symbol, "type": type(result).__name__, "message": str(result)}
                )
                continue
            bars.extend(bar for bar in result if start <= bar.trade_date <= end)
        if not bars:
            raise RuntimeError(f"Yahoo did not return index OHLC rows: {errors}")

        await repo.upsert_equity_rows(bars)
        await repo.upsert_discovered_symbols(
            [{"symbol": symbol, "symbol_type": "index"} for symbol in symbols]
        )

        settings = get_settings()
        pipeline = Pipeline(
            settings=settings,
            repository=repo,
            bhavcopy_source=build_bhavcopy_source(settings),
            rates=IndiaRiskFreeRateClient(settings.default_risk_free_rate),
        )
        dates = [
            row["trade_date"]
            for row in await pool.fetch(
                """
                SELECT DISTINCT o.trade_date
                FROM options_historical o
                JOIN equity_historical e USING (symbol, trade_date)
                WHERE o.instrument_type = 'OPTIDX'
                  AND o.symbol = ANY($1::text[])
                  AND o.trade_date BETWEEN $2 AND $3
                ORDER BY o.trade_date
                """,
                symbols,
                start,
                end,
            )
        ]
        recomputed = 0
        for index, trade_date in enumerate(dates, start=1):
            for symbol in symbols:
                has_data = await pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM options_historical o
                        JOIN equity_historical e USING (symbol, trade_date)
                        WHERE o.symbol = $1
                          AND o.trade_date = $2
                    )
                    """,
                    symbol,
                    trade_date,
                )
                if not has_data:
                    continue
                await pipeline.compute_symbol_day(symbol, trade_date)
                recomputed += 1
            await repo.refresh_percentiles(trade_date)
            if args.progress_every and index % args.progress_every == 0:
                print(
                    json.dumps(
                        {
                            "event": "index_history_backfill_progress",
                            "trade_date": trade_date,
                            "dates_done": index,
                            "dates_total": len(dates),
                            "symbol_days_recomputed": recomputed,
                        },
                        default=str,
                    ),
                    flush=True,
                )
        await repo.refresh_aggregates()
        print(
            json.dumps(
                {
                    "event": "index_history_backfill_done",
                    "start": start,
                    "end": end,
                    "symbols": symbols,
                    "yahoo_rows": len(bars),
                    "yahoo_errors": errors,
                    "dates": len(dates),
                    "symbol_days_recomputed": recomputed,
                },
                default=str,
            ),
            flush=True,
        )
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
