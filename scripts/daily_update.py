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
from app.etl.pipeline import Pipeline
from app.services.factory import build_bhavcopy_source
from app.sources.nse_events import NSECorporateEventsClient
from app.sources.nse_metadata import NSEMetadataClient
from app.sources.rates import IndiaRiskFreeRateClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily EOD update after NSE bhavcopy is available.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Trade date YYYY-MM-DD.")
    parser.add_argument("--symbols", help="Optional comma-separated symbols. Omit for all F&O symbols.")
    parser.add_argument("--skip-events", action="store_true")
    args = parser.parse_args()

    trade_date = date.fromisoformat(args.date)
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",")] if args.symbols else None
    settings = get_settings()
    repo = MarketRepository(await get_pool())
    pipeline = Pipeline(
        settings=settings,
        repository=repo,
        bhavcopy_source=build_bhavcopy_source(settings),
        rates=IndiaRiskFreeRateClient(settings.default_risk_free_rate),
    )
    try:
        result = await pipeline.run_for_date(trade_date, symbols, finalize=True)
        await repo.upsert_trading_calendar([{"trade_date": trade_date, "is_trading_day": True, "source": "daily_update"}])

        active_symbols = await repo.active_symbols()
        metadata = await NSEMetadataClient(
            settings.nse_request_delay_seconds,
            settings.source_retry_attempts,
            settings.source_retry_base_delay_seconds,
            settings.source_retry_max_delay_seconds,
        ).fetch_metadata(set(active_symbols), enrich_quote=False)
        metadata_count = await repo.upsert_symbol_metadata(metadata)

        events_count = 0
        if not args.skip_events:
            event_symbols = symbols or active_symbols
            events_count = await repo.upsert_events(
                await NSECorporateEventsClient(settings.nse_request_delay_seconds).fetch_result_events(event_symbols)
            )

        print(
            json.dumps(
                {
                    "event": "daily_update_done",
                    **result,
                    "metadata_upserted": metadata_count,
                    "events_upserted": events_count,
                },
                default=str,
            )
        )
    except Exception as exc:
        await repo.log_error(
            "daily_update",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            trade_date=trade_date,
            source="daily_update",
        )
        raise
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
