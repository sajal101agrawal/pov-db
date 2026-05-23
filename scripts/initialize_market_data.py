from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.services.factory import build_bhavcopy_source
from app.sources.nse import NSEArchiveClient
from app.sources.nse_events import NSECorporateEventsClient
from app.sources.yahoo_events import YahooEarningsCalendarClient
from app.sources.nse_metadata import NSEMetadataClient
from app.sources.rates import IndiaRiskFreeRateClient


def business_days(start: date, end: date) -> list[date]:
    current = start
    days = []
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


async def populate_calendar(repo: MarketRepository, start: date, end: date) -> dict:
    rows = await repo.pool.fetch(
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
    trading = {row["trade_date"]: dict(row) for row in rows}
    calendar_rows = []
    current = start
    while current <= end:
        if current in trading:
            source = (
                "equity_options_historical"
                if trading[current]["has_equity"] and trading[current]["has_options"]
                else "partial_local_bhavcopy"
            )
            calendar_rows.append({"trade_date": current, "is_trading_day": True, "source": source})
        elif current.weekday() >= 5:
            calendar_rows.append({"trade_date": current, "is_trading_day": False, "source": "weekend"})
        else:
            calendar_rows.append({"trade_date": current, "is_trading_day": False, "source": "no_local_bhavcopy"})
        current += timedelta(days=1)
    return {
        "rows": await repo.upsert_trading_calendar(calendar_rows),
        "trading_days": len(trading),
        "partial_trading_days": sum(1 for row in trading.values() if not (row["has_equity"] and row["has_options"])),
    }


async def refresh_universe(repo: MarketRepository, trade_date: date, enrich_quote: bool) -> dict:
    settings = get_settings()
    fo_rows = await NSEArchiveClient(
        settings.nse_request_delay_seconds,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    ).fetch_fo(trade_date)
    symbols: dict[str, str] = {}
    for row in fo_rows:
        symbols[row.symbol] = "index" if row.instrument_type == "OPTIDX" else "individual_securities"
    await repo.pool.execute("UPDATE symbol_universe SET is_active = FALSE, updated_at = NOW()")
    discovered = await repo.upsert_discovered_symbols(
        [{"symbol": symbol, "symbol_type": symbol_type} for symbol, symbol_type in sorted(symbols.items())]
    )
    metadata = await NSEMetadataClient(
        settings.nse_request_delay_seconds,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    ).fetch_metadata(set(symbols), enrich_quote=enrich_quote)
    metadata_count = await repo.upsert_symbol_metadata(metadata)
    return {"trade_date": trade_date.isoformat(), "active_symbols": len(symbols), "discovered": discovered, "metadata": metadata_count}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize all market-data tables for a fresh server.")
    parser.add_argument("--start", help="Start date YYYY-MM-DD. Defaults to --years back.")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--years", type=float, default=5.0)
    parser.add_argument("--symbols", help="Optional comma-separated symbols. Omit for all F&O symbols.")
    parser.add_argument("--force", action="store_true", help="Reload dates even when option data already exists.")
    parser.add_argument("--min-existing-options", type=int, default=10_000)
    parser.add_argument("--enrich-quote", action="store_true", help="Fetch per-symbol NSE quote metadata.")
    parser.add_argument("--skip-events", action="store_true")
    parser.add_argument("--log-file", default="data/initialize_market_data.jsonl")
    args = parser.parse_args()

    end = date.fromisoformat(args.end)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=int(args.years * 365.25))
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",")] if args.symbols else None
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    repo = MarketRepository(await get_pool())
    pipeline = Pipeline(
        settings=settings,
        repository=repo,
        bhavcopy_source=build_bhavcopy_source(settings),
        rates=IndiaRiskFreeRateClient(settings.default_risk_free_rate),
    )

    def emit(record: dict) -> None:
        payload = {"ts": datetime.now(UTC).isoformat(), **record}
        print(json.dumps(payload, default=str), flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")

    try:
        days = business_days(start, end)
        emit({"event": "init_start", "start": start, "end": end, "business_days": len(days), "symbols": symbols or "ALL"})
        for idx, trade_date in enumerate(days, start=1):
            existing = int(
                await repo.pool.fetchval("SELECT COUNT(*) FROM options_historical WHERE trade_date = $1", trade_date)
                or 0
            )
            if not args.force and existing >= args.min_existing_options:
                emit({"event": "skip_existing", "trade_date": trade_date, "index": idx, "total": len(days), "options": existing})
                continue
            try:
                result = await pipeline.run_for_date(trade_date, symbols, finalize=False)
                emit({"event": "loaded", "index": idx, "total": len(days), **result})
            except Exception as exc:  # noqa: BLE001 - log and keep the bootstrap resumable
                await repo.log_error(
                    "initialize_market_data",
                    type(exc).__name__,
                    {"message": str(exc), "repr": repr(exc), "index": idx, "total": len(days)},
                    trade_date=trade_date,
                    source="initialize_market_data",
                )
                emit({"event": "failed", "trade_date": trade_date, "index": idx, "total": len(days), "error": repr(exc)})

        latest = await repo.latest_trade_date()
        if latest:
            emit({"event": "refresh_universe_start", "trade_date": latest})
            emit({"event": "refresh_universe_done", **await refresh_universe(repo, latest, args.enrich_quote)})

        emit({"event": "populate_calendar_start", "start": start, "end": end})
        emit({"event": "populate_calendar_done", **await populate_calendar(repo, start, end)})

        if not args.skip_events:
            event_symbols = symbols or await repo.active_symbols()
            nse_events = await NSECorporateEventsClient(settings.nse_request_delay_seconds).fetch_result_events(
                event_symbols
            )
            yahoo_symbols = await repo.yahoo_symbols_for(event_symbols)
            yahoo_events = await YahooEarningsCalendarClient(
                request_delay_seconds=settings.nse_request_delay_seconds
            ).fetch_upcoming_result_events(event_symbols, yahoo_symbols)
            emit(
                {
                    "event": "events_done",
                    "symbols": len(event_symbols),
                    "nse_rows": len(nse_events),
                    "yahoo_rows": len(yahoo_events),
                    "rows": await repo.upsert_events([*nse_events, *yahoo_events]),
                }
            )

        final_trade_date = await repo.latest_trade_date()
        if final_trade_date:
            await repo.refresh_percentiles(final_trade_date)
        await repo.refresh_aggregates()
        emit({"event": "init_done", "latest_trade_date": final_trade_date})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
