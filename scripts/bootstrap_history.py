from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.sources.rates import IndiaRiskFreeRateClient
from app.services.factory import build_bhavcopy_source


def business_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


async def existing_option_count(repo: MarketRepository, trade_date: date) -> int:
    return int(
        await repo.pool.fetchval(
            "SELECT COUNT(*) FROM options_historical WHERE trade_date = $1",
            trade_date,
        )
        or 0
    )


async def quality_snapshot(repo: MarketRepository, since: date, until: date) -> dict:
    totals = await repo.pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE table_name = 'options') AS options_rows,
            COUNT(DISTINCT trade_date) FILTER (WHERE table_name = 'options') AS option_dates,
            COUNT(*) FILTER (WHERE table_name = 'equity') AS equity_rows,
            COUNT(DISTINCT trade_date) FILTER (WHERE table_name = 'equity') AS equity_dates,
            COUNT(*) FILTER (WHERE table_name = 'metrics') AS metric_rows,
            COUNT(DISTINCT trade_date) FILTER (WHERE table_name = 'metrics') AS metric_dates
        FROM (
            SELECT 'options' AS table_name, trade_date FROM options_historical
            WHERE trade_date BETWEEN $1 AND $2
            UNION ALL
            SELECT 'equity' AS table_name, trade_date FROM equity_historical
            WHERE trade_date BETWEEN $1 AND $2
            UNION ALL
            SELECT 'metrics' AS table_name, trade_date FROM symbol_daily_metrics
            WHERE trade_date BETWEEN $1 AND $2
        ) all_rows
        """,
        since,
        until,
    )
    latest = await repo.pool.fetchrow(
        """
        WITH per_date AS (
            SELECT trade_date, COUNT(*) AS option_rows
            FROM options_historical
            WHERE trade_date BETWEEN $1 AND $2
            GROUP BY trade_date
            HAVING COUNT(*) >= 10000
        ),
        latest AS (
            SELECT MAX(trade_date) AS trade_date FROM per_date
        )
        SELECT
            latest.trade_date,
            COUNT(o.*) AS option_rows,
            COUNT(o.*) FILTER (WHERE o.iv IS NOT NULL) AS option_iv_rows,
            COUNT(o.*) FILTER (WHERE o.delta IS NOT NULL) AS option_delta_rows,
            COUNT(o.*) FILTER (WHERE o.iv < 0 OR o.iv > 5) AS invalid_iv_rows,
            COUNT(DISTINCT o.symbol) AS option_symbols,
            (SELECT COUNT(*) FROM equity_historical e WHERE e.trade_date = latest.trade_date) AS equity_rows,
            (SELECT COUNT(*) FROM symbol_daily_metrics m WHERE m.trade_date = latest.trade_date) AS metric_rows,
            (SELECT COUNT(*) FROM symbol_daily_metrics m WHERE m.trade_date = latest.trade_date AND m.iv_30 IS NULL) AS metric_iv30_null_rows,
            (SELECT COUNT(*) FROM symbol_daily_metrics m WHERE m.trade_date = latest.trade_date AND m.skew_25 IS NULL) AS metric_skew25_null_rows
        FROM latest
        LEFT JOIN options_historical o ON o.trade_date = latest.trade_date
        GROUP BY latest.trade_date
        """,
        since,
        until,
    )
    return {
        "totals": dict(totals) if totals else {},
        "latest_date_quality": dict(latest) if latest else {},
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap NSE F&O history date-by-date.")
    parser.add_argument("--start", help="Start date YYYY-MM-DD. Defaults to --years back.")
    parser.add_argument("--end", default=date.today().isoformat(), help="End date YYYY-MM-DD.")
    parser.add_argument("--years", type=float, default=5.0, help="Years back when --start is omitted.")
    parser.add_argument("--symbols", help="Optional comma-separated symbol filter. Omit for all F&O symbols.")
    parser.add_argument("--force", action="store_true", help="Re-run dates even when rows already exist.")
    parser.add_argument(
        "--min-existing-options",
        type=int,
        default=10_000,
        help="Skip a date only when it already has at least this many option rows.",
    )
    parser.add_argument("--log-file", default="data/bootstrap_history.jsonl")
    parser.add_argument(
        "--quality-every",
        type=int,
        default=10,
        help="Emit a data-quality snapshot after every N processed dates. Use 0 to disable.",
    )
    args = parser.parse_args()

    end = date.fromisoformat(args.end)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=int(args.years * 365.25))
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

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

    days = business_days(start, end)
    print(
        json.dumps(
            {
                "event": "bootstrap_start",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "business_days": len(days),
                "symbols": symbols or "ALL",
                "log_file": str(log_path),
            },
            default=str,
        ),
        flush=True,
    )

    try:
        for idx, trade_date in enumerate(days, start=1):
            existing = await existing_option_count(repo, trade_date)
            if not args.force and existing >= args.min_existing_options:
                record = {
                    "ts": datetime.utcnow().isoformat(),
                    "event": "skip_existing",
                    "trade_date": trade_date.isoformat(),
                    "existing_options": existing,
                    "index": idx,
                    "total": len(days),
                }
                print(json.dumps(record, default=str), flush=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
                continue

            try:
                result = await pipeline.run_for_date(trade_date, symbols, finalize=False)
                record = {
                    "ts": datetime.utcnow().isoformat(),
                    "event": "loaded",
                    "index": idx,
                    "total": len(days),
                    **result,
                }
            except Exception as exc:  # noqa: BLE001 - keep bootstrap moving across holidays/source gaps.
                await repo.log_error(
                    "bootstrap_history",
                    type(exc).__name__,
                    {"message": str(exc), "repr": repr(exc), "index": idx, "total": len(days)},
                    trade_date=trade_date,
                    source="bhavcopy_pipeline",
                )
                record = {
                    "ts": datetime.utcnow().isoformat(),
                    "event": "failed",
                    "trade_date": trade_date.isoformat(),
                    "index": idx,
                    "total": len(days),
                    "error": repr(exc),
                }

            print(json.dumps(record, default=str), flush=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")

            if args.quality_every and idx % args.quality_every == 0:
                snapshot = {
                    "ts": datetime.utcnow().isoformat(),
                    "event": "quality_snapshot",
                    "index": idx,
                    "total": len(days),
                    **await quality_snapshot(repo, start, end),
                }
                print(json.dumps(snapshot, default=str), flush=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(snapshot, default=str) + "\n")

        final_trade_date = await repo.latest_trade_date()
        if final_trade_date:
            await repo.refresh_percentiles(final_trade_date)
        await repo.refresh_aggregates()
        done = {
            "ts": datetime.utcnow().isoformat(),
            "event": "bootstrap_done",
            "latest_trade_date": final_trade_date.isoformat() if final_trade_date else None,
            **await quality_snapshot(repo, start, end),
        }
        print(json.dumps(done, default=str), flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(done, default=str) + "\n")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
