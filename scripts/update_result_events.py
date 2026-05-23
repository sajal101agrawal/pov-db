from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.sources.nse_events import NSECorporateEventsClient
from app.sources.yahoo_events import YahooEarningsCalendarClient


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh result/earnings events without running market-data ETL."
    )
    parser.add_argument("--symbols", help="Optional comma-separated symbols. Omit for active F&O symbols.")
    parser.add_argument("--skip-nse", action="store_true", help="Skip NSE filed result events.")
    parser.add_argument("--skip-yahoo", action="store_true", help="Skip Yahoo upcoming earnings dates.")
    args = parser.parse_args()

    if args.skip_nse and args.skip_yahoo:
        parser.error("at least one source must be enabled")

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    symbols: list[str] = []

    try:
        symbols = (
            [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
            if args.symbols
            else await repo.active_symbols()
        )

        nse_events = []
        if not args.skip_nse:
            nse_events = await NSECorporateEventsClient(
                settings.nse_request_delay_seconds,
                settings.source_retry_attempts,
                settings.source_retry_base_delay_seconds,
                settings.source_retry_max_delay_seconds,
            ).fetch_result_events(symbols)

        yahoo_events = []
        yahoo_events_deleted = 0
        if not args.skip_yahoo:
            yahoo_symbols = await repo.yahoo_symbols_for(symbols)
            yahoo_events = await YahooEarningsCalendarClient(
                request_delay_seconds=settings.nse_request_delay_seconds,
                retry_attempts=settings.source_retry_attempts,
                retry_base_delay_seconds=settings.source_retry_base_delay_seconds,
                retry_max_delay_seconds=settings.source_retry_max_delay_seconds,
            ).fetch_upcoming_result_events(symbols, yahoo_symbols)
            yahoo_events_deleted = await repo.delete_future_events_by_source(
                symbols,
                "yahoo:earnings-calendar",
                date.today(),
            )

        rows_upserted = await repo.upsert_events([*nse_events, *yahoo_events])
        print(
            json.dumps(
                {
                    "event": "result_events_updated",
                    "symbols": len(symbols),
                    "nse_rows": len(nse_events),
                    "yahoo_rows": len(yahoo_events),
                    "yahoo_deleted": yahoo_events_deleted,
                    "rows_upserted": rows_upserted,
                },
                default=str,
            )
        )
    except Exception as exc:
        await repo.log_error(
            "update_result_events",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            source="update_result_events",
        )
        raise
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
