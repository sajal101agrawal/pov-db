from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date
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
from app.services.calculations import iv_slope, ratio
from app.services.forward_factors import compute_forward_factor_metrics


FORWARD_FIELDS = (
    "iv_30",
    "iv_60",
    "iv_90",
    "call_iv_30",
    "call_iv_60",
    "call_iv_90",
    "put_iv_30",
    "put_iv_60",
    "put_iv_90",
    "atm_strike",
    "nearest_ce_iv",
    "nearest_pe_iv",
    "nearest_ce_ltp",
    "nearest_pe_ltp",
    "expiry_30d",
    "expiry_60d",
    "expiry_90d",
    "dte_30",
    "dte_60",
    "dte_90",
    "fwdv_3060",
    "fwdfct_3060",
    "call_fwdfct_3060",
    "put_fwdfct_3060",
    "fev_30",
    "iv_slope_3060",
    "iv30_fev30_ratio",
)

DATE_FORWARD_FIELDS = {"expiry_30d", "expiry_60d", "expiry_90d"}
INT_FORWARD_FIELDS = {"dte_30", "dte_60", "dte_90"}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill separate call/put ATM IV term structures and Forward Factors. "
            "Without --execute this is read-only."
        )
    )
    parser.add_argument("--start", help="First metric date YYYY-MM-DD. Defaults to earliest.")
    parser.add_argument("--end", help="Last metric date YYYY-MM-DD. Defaults to latest.")
    parser.add_argument("--symbols", help="Optional comma-separated symbol filter.")
    parser.add_argument("--execute", action="store_true", help="Apply the backfill.")
    parser.add_argument("--progress-every", type=int, default=10)
    args = parser.parse_args()

    settings = get_settings()
    pool = await get_pool()
    repository = MarketRepository(pool)
    requested_symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    bounds = await pool.fetchrow(
        """
        SELECT MIN(trade_date) AS min_date, MAX(trade_date) AS max_date, COUNT(*) AS rows
        FROM symbol_daily_metrics
        WHERE ($1::text[] IS NULL OR symbol = ANY($1::text[]))
        """,
        requested_symbols,
    )
    if not bounds or bounds["min_date"] is None:
        raise RuntimeError("No symbol_daily_metrics rows found for the selected symbols.")
    start = date.fromisoformat(args.start) if args.start else bounds["min_date"]
    end = date.fromisoformat(args.end) if args.end else bounds["max_date"]
    symbols = [
        row["symbol"]
        for row in await pool.fetch(
            """
            SELECT DISTINCT symbol
            FROM symbol_daily_metrics
            WHERE trade_date BETWEEN $1 AND $2
              AND ($3::text[] IS NULL OR symbol = ANY($3::text[]))
            ORDER BY symbol
            """,
            start,
            end,
            requested_symbols,
        )
    ]
    metric_count = int(
        await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM symbol_daily_metrics
            WHERE trade_date BETWEEN $1 AND $2
              AND ($3::text[] IS NULL OR symbol = ANY($3::text[]))
            """,
            start,
            end,
            requested_symbols,
        )
        or 0
    )
    preview = {
        "event": "forward_factor_backfill_preview",
        "start": start,
        "end": end,
        "symbols": len(symbols),
        "metric_rows": metric_count,
        "execute": args.execute,
    }
    print(json.dumps(preview, default=str), flush=True)
    if not args.execute:
        print("Read-only preview complete. Re-run with --execute to apply changes.", flush=True)
        await close_pool()
        return

    run_id = uuid4()
    await pool.execute(
        """
        INSERT INTO analytics_backfill_runs (run_id, status, parameters)
        VALUES ($1, 'RUNNING', $2::jsonb)
        """,
        run_id,
        json.dumps(preview, default=str),
    )
    summary: dict[str, Any] = {}
    try:
        updated = 0
        audited = 0
        skipped_no_chain = 0
        updated_dates: set[date] = set()
        for symbol_index, symbol in enumerate(symbols, start=1):
            metric_rows = [
                dict(row)
                for row in await pool.fetch(
                    f"""
                    SELECT sdm.symbol, sdm.trade_date, eh.close::float AS spot_close,
                           {', '.join(_metric_select_expression(field) for field in FORWARD_FIELDS)}
                    FROM symbol_daily_metrics sdm
                    JOIN equity_historical eh
                      ON eh.symbol = sdm.symbol AND eh.trade_date = sdm.trade_date
                    WHERE sdm.symbol = $1 AND sdm.trade_date BETWEEN $2 AND $3
                    ORDER BY sdm.trade_date
                    """,
                    symbol,
                    start,
                    end,
                )
            ]
            if not metric_rows:
                continue
            option_rows = await _atm_option_rows(pool, symbol, start, end)
            chains: dict[date, list[dict[str, Any]]] = defaultdict(list)
            for row in option_rows:
                chains[row["trade_date"]].append(dict(row))

            updates: list[tuple[Any, ...]] = []
            audits: list[tuple[Any, ...]] = []
            for old in metric_rows:
                chain = chains.get(old["trade_date"], [])
                if not chain:
                    skipped_no_chain += 1
                    continue
                calculated = compute_forward_factor_metrics(
                    chain, old["trade_date"], old["spot_close"]
                )
                calculated["fev_30"] = calculated.get("fwdv_3060")
                calculated["iv_slope_3060"] = iv_slope(
                    calculated.get("iv_30"),
                    calculated.get("iv_60"),
                    calculated.get("dte_30") or 30,
                    calculated.get("dte_60") or 60,
                )
                calculated["iv30_fev30_ratio"] = ratio(
                    calculated.get("iv_30"),
                    calculated.get("fev_30"),
                )
                new_values = {field: calculated.get(field) for field in FORWARD_FIELDS}
                if not materially_changed(old, new_values):
                    continue
                updates.append(
                    (symbol, old["trade_date"], *(new_values[field] for field in FORWARD_FIELDS))
                )
                updated_dates.add(old["trade_date"])
                audits.append(
                    (
                        run_id,
                        symbol,
                        old["trade_date"],
                        json.dumps(audit_values(old), default=str),
                        json.dumps(audit_values(new_values), default=str),
                    )
                )

            if updates:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(_UPDATE_SQL, updates)
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
                            "event": "forward_factor_backfill_progress",
                            "symbols_done": symbol_index,
                            "symbols_total": len(symbols),
                            "updated": updated,
                        }
                    ),
                    flush=True,
                )

        for percentile_date in sorted(updated_dates):
            await repository.refresh_percentiles(percentile_date)

        cache_keys_deleted = await invalidate_forward_factor_cache(settings.redis_url)
        summary.update(
            updated=updated,
            audited_changes=audited,
            skipped_no_chain=skipped_no_chain,
            percentile_dates_refreshed=len(updated_dates),
            cache_keys_deleted=cache_keys_deleted,
        )
        await pool.execute(
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
                {"event": "forward_factor_backfill_done", "run_id": run_id, **summary},
                default=str,
            ),
            flush=True,
        )
    except Exception as exc:
        summary["error"] = repr(exc)
        await pool.execute(
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


async def _atm_option_rows(pool: Any, symbol: str, start: date, end: date) -> list[Any]:
    return await pool.fetch(
        """
        WITH metric_dates AS (
            SELECT sdm.trade_date, eh.close::float AS spot_close
            FROM symbol_daily_metrics sdm
            JOIN equity_historical eh
              ON eh.symbol = sdm.symbol AND eh.trade_date = sdm.trade_date
            WHERE sdm.symbol = $1 AND sdm.trade_date BETWEEN $2 AND $3
        ),
        monthly_expiries AS (
            SELECT md.trade_date,
                   MAX(oh.expiry_date) AS expiry_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY md.trade_date
                       ORDER BY date_trunc('month', oh.expiry_date)
                   ) AS expiry_rank
            FROM metric_dates md
            JOIN options_historical oh
              ON oh.symbol = $1
             AND oh.trade_date = md.trade_date
             AND oh.expiry_date > md.trade_date
            GROUP BY md.trade_date, date_trunc('month', oh.expiry_date)
        ),
        selected_expiries AS (
            SELECT trade_date, expiry_date, expiry_rank
            FROM monthly_expiries
            WHERE expiry_rank <= 3
        ),
        near_strikes AS (
            SELECT DISTINCT md.trade_date, md.spot_close, oh.strike_price
            FROM metric_dates md
            JOIN selected_expiries se
              ON se.trade_date = md.trade_date
             AND se.expiry_rank = 1
            JOIN options_historical oh
              ON oh.symbol = $1
             AND oh.trade_date = md.trade_date
             AND oh.expiry_date = se.expiry_date
        ),
        ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY trade_date
                ORDER BY ABS(strike_price - spot_close), strike_price
            ) AS strike_rank
            FROM near_strikes
        )
        SELECT oh.trade_date, oh.expiry_date, oh.strike_price::float,
               oh.option_type, oh.iv::float, oh.settle_price::float, oh.close::float
        FROM ranked atm
        JOIN selected_expiries se
          ON se.trade_date = atm.trade_date
        JOIN options_historical oh
          ON oh.symbol = $1
         AND oh.trade_date = atm.trade_date
         AND oh.expiry_date = se.expiry_date
         AND oh.strike_price = atm.strike_price
         AND oh.option_type IN ('CE', 'PE')
        WHERE atm.strike_rank = 1
        ORDER BY oh.trade_date, oh.expiry_date, oh.option_type
        """,
        symbol,
        start,
        end,
    )


def _metric_select_expression(field: str) -> str:
    if field in DATE_FORWARD_FIELDS or field in INT_FORWARD_FIELDS:
        return f"sdm.{field}"
    return f"sdm.{field}::float"


def audit_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        field: _audit_value(values.get(field))
        for field in FORWARD_FIELDS
    }


def _audit_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        return value
    return round(float(value), 8)


def materially_changed(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return audit_values(old) != audit_values(new)


async def invalidate_forward_factor_cache(redis_url: str) -> int:
    redis = Redis.from_url(redis_url)
    keys: list[Any] = []
    deleted = 0
    try:
        for pattern in ("dashboard:*", "history:*", "all_dashboard:*", "term_structure:*"):
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


_UPDATE_SQL = f"""
    UPDATE symbol_daily_metrics
    SET {', '.join(f'{field} = ${index}' for index, field in enumerate(FORWARD_FIELDS, start=3))},
        updated_at = NOW()
    WHERE symbol = $1 AND trade_date = $2
"""


if __name__ == "__main__":
    asyncio.run(main())
