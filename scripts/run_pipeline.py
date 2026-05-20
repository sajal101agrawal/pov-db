from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.sources.rates import IndiaRiskFreeRateClient
from app.services.factory import build_bhavcopy_source


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Trade date in YYYY-MM-DD format")
    parser.add_argument("--symbols", help="Comma-separated symbol filter")
    parser.add_argument("--no-finalize", action="store_true", help="Skip percentile and aggregate refresh.")
    args = parser.parse_args()

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    pipeline = Pipeline(
        settings=settings,
        repository=repo,
        bhavcopy_source=build_bhavcopy_source(settings),
        rates=IndiaRiskFreeRateClient(settings.default_risk_free_rate),
    )
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    trade_date = date.fromisoformat(args.date)
    try:
        print(await pipeline.run_for_date(trade_date, symbols, finalize=not args.no_finalize))
    except Exception as exc:
        await repo.log_error(
            "run_pipeline",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            trade_date=trade_date,
            source="manual_pipeline",
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())
