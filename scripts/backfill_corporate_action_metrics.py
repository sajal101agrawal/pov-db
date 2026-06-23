from __future__ import annotations

import argparse
import asyncio
from bisect import bisect_right
from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository
from app.services.calculations import ratio, volatility_risk_premium
from app.services.corporate_actions import USABLE_RV_STATUSES, calculate_price_series_metrics
from app.services.factory import build_corporate_actions_source


NUMERIC_AUDIT_FIELDS = (
    "rv_10",
    "rv_20",
    "rv_30",
    "rv_60",
    "rv_90",
    "rv_10_raw",
    "rv_20_raw",
    "rv_30_raw",
    "rv_60_raw",
    "rv_90_raw",
    "vrp",
    "iv30_rv30_ratio",
    "daily_rsi",
    "weekly_rsi",
)

NUMERIC_AUDIT_SCALES = {
    "rv_10": 8,
    "rv_20": 8,
    "rv_30": 8,
    "rv_60": 8,
    "rv_90": 8,
    "rv_10_raw": 8,
    "rv_20_raw": 8,
    "rv_30_raw": 8,
    "rv_60_raw": 8,
    "rv_90_raw": 8,
    "vrp": 8,
    "iv30_rv30_ratio": 8,
    "daily_rsi": 4,
    "weekly_rsi": 4,
}

STATE_AUDIT_FIELDS = (
    "rv_data_status",
    "rv_calculation_version",
    "vrp_signal_enabled",
)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute corporate-action-safe RV, VRP, RSI, percentiles, and aggregates "
            "from existing raw production rows. Without --execute this is read-only."
        )
    )
    parser.add_argument("--start", help="First metric date YYYY-MM-DD. Defaults to earliest.")
    parser.add_argument("--end", help="Last metric date YYYY-MM-DD. Defaults to latest.")
    parser.add_argument("--symbols", help="Optional comma-separated symbol filter.")
    parser.add_argument("--execute", action="store_true", help="Apply the backfill.")
    parser.add_argument("--skip-action-sync", action="store_true")
    parser.add_argument("--progress-every", type=int, default=10)
    args = parser.parse_args()

    settings = get_settings()
    repo = MarketRepository(await get_pool())
    symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else [
            row["symbol"]
            for row in await repo.pool.fetch(
                "SELECT DISTINCT symbol FROM symbol_daily_metrics ORDER BY symbol"
            )
        ]
    )
    bounds = await repo.pool.fetchrow(
        """
        SELECT MIN(trade_date) AS min_date, MAX(trade_date) AS max_date, COUNT(*) AS rows
        FROM symbol_daily_metrics
        WHERE ($1::text[] IS NULL OR symbol = ANY($1::text[]))
        """,
        symbols or None,
    )
    if not bounds or bounds["min_date"] is None:
        raise RuntimeError("No symbol_daily_metrics rows found for the selected symbols.")
    start = date.fromisoformat(args.start) if args.start else bounds["min_date"]
    end = date.fromisoformat(args.end) if args.end else bounds["max_date"]
    selected_count = int(
        await repo.pool.fetchval(
            """
            SELECT COUNT(*)
            FROM symbol_daily_metrics
            WHERE trade_date BETWEEN $1 AND $2
              AND symbol = ANY($3::text[])
            """,
            start,
            end,
            symbols,
        )
        or 0
    )
    existing_actions = int(
        await repo.pool.fetchval(
            """
            SELECT COUNT(*)
            FROM corporate_actions
            WHERE ex_date BETWEEN $1 AND $2
              AND symbol = ANY($3::text[])
            """,
            start - timedelta(days=365),
            end,
            symbols,
        )
        or 0
    )
    preview = {
        "event": "corporate_action_backfill_preview",
        "start": start,
        "end": end,
        "symbols": len(symbols),
        "metric_rows": selected_count,
        "existing_corporate_actions": existing_actions,
        "will_sync_actions": not args.skip_action_sync,
        "execute": args.execute,
    }
    print(json.dumps(preview, default=str), flush=True)
    if not args.execute:
        print("Read-only preview complete. Re-run with --execute to apply changes.", flush=True)
        await close_pool()
        return

    run_id = uuid4()
    await repo.pool.execute(
        """
        INSERT INTO analytics_backfill_runs (run_id, status, parameters)
        VALUES ($1, 'RUNNING', $2::jsonb)
        """,
        run_id,
        json.dumps(preview, default=str),
    )
    summary: dict[str, Any] = {}
    try:
        if not args.skip_action_sync:
            source = build_corporate_actions_source(settings)
            sync_start = start - timedelta(days=365)
            actions = await source.fetch_actions(sync_start, end, symbols)
            summary["actions_fetched"] = len(actions)
            summary["actions_upserted"] = await repo.upsert_corporate_actions(actions)
        summary["factor_resolution"] = await repo.resolve_corporate_action_factors(
            start=start - timedelta(days=365), end=end, symbols=symbols
        )

        updated = 0
        audited = 0
        statuses: Counter[str] = Counter()
        for symbol_index, symbol in enumerate(symbols, start=1):
            metric_rows = [
                dict(row)
                for row in await repo.pool.fetch(
                    """
                    SELECT symbol, trade_date, iv_30::float,
                           rv_10::float, rv_20::float, rv_30::float,
                           rv_60::float, rv_90::float, vrp::float,
                           rv_10_raw::float, rv_20_raw::float, rv_30_raw::float,
                           rv_60_raw::float, rv_90_raw::float,
                           iv30_rv30_ratio::float, daily_rsi::float, weekly_rsi::float,
                           rv_data_status, rv_calculation_version, vrp_signal_enabled
                    FROM symbol_daily_metrics
                    WHERE symbol = $1 AND trade_date <= $2
                    ORDER BY trade_date
                    """,
                    symbol,
                    end,
                )
            ]
            if not metric_rows:
                continue
            equity_rows = [
                dict(row)
                for row in await repo.pool.fetch(
                    """
                    SELECT trade_date, open::float, high::float, low::float, close::float
                    FROM equity_historical
                    WHERE symbol = $1 AND trade_date <= $2
                    ORDER BY trade_date
                    """,
                    symbol,
                    end,
                )
            ]
            if not equity_rows:
                continue
            action_rows = await repo.corporate_actions_window(
                symbol, equity_rows[0]["trade_date"] - timedelta(days=1), end
            )
            equity_dates = [row["trade_date"] for row in equity_rows]
            iv_history: list[float] = []
            updates: list[tuple[Any, ...]] = []
            audits: list[tuple[Any, ...]] = []
            for old in metric_rows:
                trade_date = old["trade_date"]
                lagged_iv30 = iv_history[-20] if len(iv_history) >= 20 else None
                if old.get("iv_30") is not None:
                    iv_history.append(float(old["iv_30"]))
                if trade_date < start:
                    continue
                position = bisect_right(equity_dates, trade_date)
                ohlc = equity_rows[max(0, position - 100) : position]
                if not ohlc:
                    continue
                price_metrics = calculate_price_series_metrics(ohlc, action_rows, trade_date)
                rv_status = price_metrics["rv_data_status"]
                vrp = (
                    volatility_risk_premium(lagged_iv30, price_metrics["rv_30"])
                    if rv_status in USABLE_RV_STATUSES
                    else None
                )
                new = {
                    **price_metrics,
                    "vrp": vrp,
                    "vrp_signal_enabled": vrp is not None,
                    "iv30_rv30_ratio": ratio(old.get("iv_30"), price_metrics["rv_30"]),
                }
                updates.append(_update_tuple(symbol, trade_date, new))
                statuses[rv_status] += 1
                if _materially_changed(old, new):
                    audits.append(
                        (
                            run_id,
                            symbol,
                            trade_date,
                            json.dumps(_audit_values(old), default=str),
                            json.dumps(_audit_values(new), default=str),
                        )
                    )

            if updates:
                async with repo.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(_UPDATE_SQL, updates)
                        if audits:
                            await conn.executemany(
                                """
                                INSERT INTO analytics_metric_audit (
                                    run_id, symbol, trade_date, old_values, new_values
                                ) VALUES ($1,$2,$3,$4::jsonb,$5::jsonb)
                                ON CONFLICT (run_id, symbol, trade_date) DO NOTHING
                                """,
                                audits,
                            )
                updated += len(updates)
                audited += len(audits)
            if args.progress_every and symbol_index % args.progress_every == 0:
                print(
                    json.dumps(
                        {
                            "event": "backfill_progress",
                            "symbols_done": symbol_index,
                            "symbols_total": len(symbols),
                            "updated": updated,
                            "audited_changes": audited,
                        }
                    ),
                    flush=True,
                )

        percentile_end = min(
            bounds["max_date"],
            end + timedelta(days=450),
        )
        percentile_dates = [
            row["trade_date"]
            for row in await repo.pool.fetch(
                """
                SELECT DISTINCT trade_date
                FROM symbol_daily_metrics
                WHERE trade_date BETWEEN $1 AND $2
                ORDER BY trade_date
                """,
                start,
                percentile_end,
            )
        ]
        for trade_date in percentile_dates:
            await repo.refresh_percentiles(trade_date)
        await repo.refresh_aggregates()
        cache_keys_deleted = await _invalidate_analytics_cache(settings.redis_url)

        summary.update(
            updated=updated,
            audited_changes=audited,
            statuses=dict(statuses),
            percentiles_refreshed=len(percentile_dates),
            cache_keys_deleted=cache_keys_deleted,
        )
        await repo.pool.execute(
            """
            UPDATE analytics_backfill_runs
            SET status = 'COMPLETED', completed_at = NOW(), summary = $2::jsonb
            WHERE run_id = $1
            """,
            run_id,
            json.dumps(summary, default=str),
        )
        print(
            json.dumps(
                {"event": "corporate_action_backfill_done", "run_id": run_id, **summary},
                default=str,
            ),
            flush=True,
        )
    except Exception as exc:
        summary["error"] = repr(exc)
        await repo.pool.execute(
            """
            UPDATE analytics_backfill_runs
            SET status = 'FAILED', completed_at = NOW(), summary = $2::jsonb
            WHERE run_id = $1
            """,
            run_id,
            json.dumps(summary, default=str),
        )
        raise
    finally:
        await close_pool()


def _update_tuple(symbol: str, trade_date: date, values: dict[str, Any]) -> tuple[Any, ...]:
    return (
        symbol,
        trade_date,
        values["rv_10"],
        values["rv_20"],
        values["rv_30"],
        values["rv_60"],
        values["rv_90"],
        values["rv_10_raw"],
        values["rv_20_raw"],
        values["rv_30_raw"],
        values["rv_60_raw"],
        values["rv_90_raw"],
        values["rv_data_status"],
        json.dumps(values["rv_adjustment_details"], default=str),
        values["rv_calculation_version"],
        values["vrp"],
        values["vrp_signal_enabled"],
        values["iv30_rv30_ratio"],
        values["daily_rsi"],
        values["weekly_rsi"],
    )


_UPDATE_SQL = """
    UPDATE symbol_daily_metrics
    SET rv_10 = $3, rv_20 = $4, rv_30 = $5, rv_60 = $6, rv_90 = $7,
        rv_10_raw = $8, rv_20_raw = $9, rv_30_raw = $10,
        rv_60_raw = $11, rv_90_raw = $12,
        rv_data_status = $13, rv_adjustment_details = $14::jsonb,
        rv_calculation_version = $15, vrp = $16, vrp_signal_enabled = $17,
        iv30_rv30_ratio = $18, daily_rsi = $19, weekly_rsi = $20,
        updated_at = NOW()
    WHERE symbol = $1 AND trade_date = $2
"""


def _materially_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if any(
        not _same_number(
            old.get(field),
            new.get(field),
            scale=NUMERIC_AUDIT_SCALES[field],
        )
        for field in NUMERIC_AUDIT_FIELDS
    ):
        return True
    return any(old.get(field) != new.get(field) for field in STATE_AUDIT_FIELDS)


def _same_number(left: Any, right: Any, *, scale: int) -> bool:
    if left is None or right is None:
        return left is right
    return round(float(left), scale) == round(float(right), scale)


def _audit_values(values: dict[str, Any]) -> dict[str, Any]:
    numeric = {
        field: (
            round(float(values[field]), NUMERIC_AUDIT_SCALES[field])
            if values.get(field) is not None
            else None
        )
        for field in NUMERIC_AUDIT_FIELDS
    }
    return {
        **numeric,
        **{field: values.get(field) for field in STATE_AUDIT_FIELDS},
    }


async def _invalidate_analytics_cache(redis_url: str) -> int:
    redis = Redis.from_url(redis_url)
    keys: list[Any] = []
    deleted = 0
    try:
        for pattern in (
            "dashboard:*",
            "history:*",
            "all_dashboard:*",
            "vol_cone:*",
            "corporate-actions:*",
        ):
            async for key in redis.scan_iter(match=pattern, count=500):
                keys.append(key)
                if len(keys) >= 500:
                    deleted += int(await redis.delete(*keys))
                    keys.clear()
        if keys:
            deleted += int(await redis.delete(*keys))
        return deleted
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
