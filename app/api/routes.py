from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.pool import get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.services.cache import CacheService
from app.services.factory import build_bhavcopy_source, build_corporate_actions_source
from app.services.live import fetch_and_store_live_quotes, fetch_and_store_live_snapshots
from app.sources.rates import IndiaRiskFreeRateClient


router = APIRouter()

ERROR_LOG_SORT_COLUMNS = {
    "created_at": "created_at",
    "trade_date": "trade_date",
    "id": "id",
    "task_name": "task_name",
    "symbol": "symbol",
    "source": "source",
    "error_type": "error_type",
    "resolved": "resolved",
}


async def repository() -> MarketRepository:
    return MarketRepository(await get_pool())


async def cache(settings: Settings = Depends(get_settings)) -> CacheService:
    return CacheService(Redis.from_url(settings.redis_url))


@router.get("/health")
async def health(repo: MarketRepository = Depends(repository)) -> dict:
    latest = await repo.latest_trade_date()
    return {"ok": True, "latest_trade_date": latest}


@router.get("/admin/error-logs")
async def error_logs(
    limit: int = Query(default=50, ge=1, le=500, description="Rows per page"),
    offset: int = Query(default=0, ge=0, description="Row offset for pagination"),
    search: str = Query(default="", description="Search task, symbol, source, type, and details"),
    task_name: str = Query(default="", description="Comma-separated task names"),
    symbol: str = Query(default="", description="Comma-separated symbols"),
    source: str = Query(default="", description="Comma-separated sources"),
    error_type: str = Query(default="", description="Comma-separated error types"),
    resolved: bool | None = Query(default=None),
    trade_date_min: date | None = Query(default=None),
    trade_date_max: date | None = Query(default=None),
    created_at_min: datetime | None = Query(default=None),
    created_at_max: datetime | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_dir: str = Query(default="desc"),
    repo: MarketRepository = Depends(repository),
) -> dict:
    where_parts = ["TRUE"]
    params: list[Any] = []
    idx = 1

    if search.strip():
        where_parts.append(
            f"""(
                task_name ILIKE ${idx}
                OR symbol ILIKE ${idx}
                OR source ILIKE ${idx}
                OR error_type ILIKE ${idx}
                OR error_details::text ILIKE ${idx}
                OR trade_date::text ILIKE ${idx}
                OR created_at::text ILIKE ${idx}
            )"""
        )
        params.append(f"%{search.strip()}%")
        idx += 1

    for column, raw_value, transform in [
        ("task_name", task_name, None),
        ("symbol", symbol, str.upper),
        ("source", source, None),
        ("error_type", error_type, None),
    ]:
        values = _csv_filter_values(raw_value, transform)
        if values:
            where_parts.append(f"{column} = ANY(${idx}::text[])")
            params.append(values)
            idx += 1

    if resolved is not None:
        where_parts.append(f"resolved = ${idx}")
        params.append(resolved)
        idx += 1

    if trade_date_min is not None:
        where_parts.append(f"trade_date >= ${idx}")
        params.append(trade_date_min)
        idx += 1
    if trade_date_max is not None:
        where_parts.append(f"trade_date <= ${idx}")
        params.append(trade_date_max)
        idx += 1
    if created_at_min is not None:
        where_parts.append(f"created_at >= ${idx}")
        params.append(created_at_min)
        idx += 1
    if created_at_max is not None:
        where_parts.append(f"created_at <= ${idx}")
        params.append(created_at_max)
        idx += 1

    sort_column = ERROR_LOG_SORT_COLUMNS.get(sort_by)
    if sort_column is None:
        raise HTTPException(status_code=400, detail="invalid sort_by")
    direction = sort_dir.lower()
    if direction not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="invalid sort_dir")

    where_clause = " AND ".join(where_parts)
    total = await repo.pool.fetchval(
        f"SELECT COUNT(*) FROM error_log WHERE {where_clause}",
        *params,
    )
    rows = await repo.pool.fetch(
        f"""
        SELECT id, task_name, symbol, trade_date, source, error_type,
               error_details::text AS error_details_json,
               created_at, resolved
        FROM error_log
        WHERE {where_clause}
        ORDER BY {sort_column} {direction.upper()} NULLS LAST, id DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params,
        limit,
        offset,
    )

    data = []
    for row in rows:
        item = dict(row)
        item["error_details"] = _json_field(item.pop("error_details_json"))
        data.append(item)

    return {
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(data) < int(total or 0),
        "sort": {"by": sort_by, "dir": direction},
        "filters": {
            "search": search.strip() or None,
            "task_name": _csv_filter_values(task_name),
            "symbol": _csv_filter_values(symbol, str.upper),
            "source": _csv_filter_values(source),
            "error_type": _csv_filter_values(error_type),
            "resolved": resolved,
            "trade_date_min": trade_date_min,
            "trade_date_max": trade_date_max,
            "created_at_min": created_at_min,
            "created_at_max": created_at_max,
        },
        "data": data,
    }


def _csv_filter_values(value: str, transform=None) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if transform:
        values = [transform(item) for item in values]
    return values


def _json_field(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


def _date_to_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


@router.get("/sectors")
async def sectors(
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    cached = await cache_service.get_json("sectors:list")
    if cached is not None:
        return cached
    rows = await repo.pool.fetch(
        """
        SELECT sector, array_agg(symbol ORDER BY symbol) AS symbols
        FROM symbol_universe
        WHERE is_active AND sector IS NOT NULL
        GROUP BY sector
        ORDER BY sector
        """
    )
    result = {"sectors": [{"sector": r["sector"], "symbols": list(r["symbols"])} for r in rows]}
    await cache_service.set_json("sectors:list", result)
    return result


@router.get("/symbol/{symbol}/events")
async def symbol_events(
    symbol: str,
    limit: int = Query(default=50, ge=1, le=500),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    cache_key = f"events:{symbol.upper()}:{limit}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    rows = await repo.pool.fetch(
        """
        SELECT symbol, event_date, event_type, description, source
        FROM events
        WHERE symbol = $1
        ORDER BY event_date DESC
        LIMIT $2
        """,
        symbol.upper(),
        limit,
    )
    result = [dict(r) for r in rows]
    await cache_service.set_json(cache_key, result)
    return result


@router.get("/symbol/{symbol}/corporate-actions")
async def symbol_corporate_actions(
    symbol: str,
    limit: int = Query(default=100, ge=1, le=500),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    cache_key = f"corporate-actions:{symbol.upper()}:{limit}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    result = await repo.corporate_actions_for_symbol(symbol, limit)
    await cache_service.set_json(cache_key, result)
    return result


@router.get("/symbol/{symbol}/expiries")
async def symbol_expiries(
    symbol: str,
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    cache_key = f"expiries:{symbol.upper()}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    rows = await repo.pool.fetch(
        """
        SELECT symbol, expiry_date, instrument_type, expiry_type
        FROM expiry_calendar
        WHERE symbol = $1
        ORDER BY expiry_date
        """,
        symbol.upper(),
    )
    result = [dict(r) for r in rows]
    await cache_service.set_json(cache_key, result)
    return result


FILTERABLE_NUMERIC = {
    "vrp":               "CASE WHEN sdm.vrp_signal_enabled THEN sdm.vrp END",
    "iv_30":             "sdm.iv_30",
    "iv_60":             "sdm.iv_60",
    "iv_90":             "sdm.iv_90",
    "iv_30_percentile":  "sdm.iv_30_percentile",
    "iv_60_percentile":  "sdm.iv_60_percentile",
    "iv_90_percentile":  "sdm.iv_90_percentile",
    "fwdv_3060":         "sdm.fwdv_3060",
    "fwdfct_3060":       "sdm.fwdfct_3060",
    "call_fwdfct_3060":  "sdm.call_fwdfct_3060",
    "put_fwdfct_3060":   "sdm.put_fwdfct_3060",
    "max_fwdfct_3060":   "GREATEST(sdm.call_fwdfct_3060, sdm.put_fwdfct_3060)",
    "iv_slope_3060":     "sdm.iv_slope_3060",
    "rv_10":             "CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_10 END",
    "rv_20":             "CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_20 END",
    "rv_30":             "CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_30 END",
    "fev_30":            "sdm.fev_30",
    "smoothed_skew":     "sdm.smoothed_skew",
    "skew_percentile":   "sdm.skew_percentile",
    "skew_rank":         "sdm.skew_rank",
    "historical_iv_crush": "sdm.historical_iv_crush",
    "implied_result_move": "sdm.implied_result_move",
    "avg_result_move":   "sa.avg_result_move",
    "max_result_move":   "sa.max_result_move",
    "daily_rsi":         "sdm.daily_rsi",
    "weekly_rsi":        "sdm.weekly_rsi",
    "nearest_ce_iv":     "sdm.nearest_ce_iv",
    "nearest_ce_ltp":    "sdm.nearest_ce_ltp",
    "nearest_pe_iv":     "sdm.nearest_pe_iv",
    "nearest_pe_ltp":    "sdm.nearest_pe_ltp",
    "avg_call_pnl":      "sa.avg_call_pnl",
    "avg_put_pnl":       "sa.avg_put_pnl",
    "iv30_rv30_ratio":   "sdm.iv30_rv30_ratio",
    "iv30_fev30_ratio":  "sdm.iv30_fev30_ratio",
    "avg_option_volume": "sdm.avg_option_volume",
    "avg_straddle_pnl":      "sa.avg_straddle_pnl",
    "avg_straddle_pnl_pct":  "sa.avg_straddle_pnl_pct",
    "avg_earnings_pnl":      "sa.avg_earnings_pnl",
    "vrp_win_rate":           "CASE WHEN sa.vrp_calculation_version >= 2 THEN sa.vrp_win_rate END",
    "avg_vrp_4y":             "CASE WHEN sa.vrp_calculation_version >= 2 THEN sa.avg_vrp_4y END",
    "max_loss":               "sa.max_loss",
    "max_profit":             "sa.max_profit",
    "current_price":          "eq.close",
}


LIVE_OVERLAY_NUMERIC_FIELDS = {
    "avg_option_volume",
    "current_price",
    "fwdv_3060",
    "fwdfct_3060",
    "call_fwdfct_3060",
    "put_fwdfct_3060",
    "max_fwdfct_3060",
    "iv_30",
    "iv_60",
    "iv_90",
    "iv_slope_3060",
    "rv_30",
    "vrp",
}


def _overlay_live_dashboard_payload(payload: dict[str, Any], live_by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "").upper()
    live = live_by_symbol.get(symbol)
    return {**payload, **live} if live else payload


def _matches_numeric_filters(payload: dict[str, Any], numeric_filters: dict[str, dict[str, float]]) -> bool:
    for field, bounds in numeric_filters.items():
        value = payload.get(field)
        if value is None:
            return False
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if "min" in bounds and number < bounds["min"]:
            return False
        if "max" in bounds and number > bounds["max"]:
            return False
    return True


@router.get("/all-dashboard")
async def all_symbols_dashboard(
    limit: int = Query(default=50, ge=1, le=200, description="Rows per page"),
    offset: int = Query(default=0, ge=0, description="Row offset for pagination"),
    search: str = Query(default="", description="Filter by symbol or company name"),
    symbol_type: str = Query(default=""),
    is_nifty50: str = Query(default=""),
    is_nifty100: str = Query(default=""),
    has_result: str = Query(default=""),
    result_date_min: str = Query(default=""),
    result_date_max: str = Query(default=""),
    sectors: str = Query(default=""),
    numeric_filters: str = Query(default=""),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    """Latest dashboard row for every active symbol — paginated screener table."""
    import json as _json

    has_any_filter = any([
        search.strip(), symbol_type.strip(), is_nifty50.strip(), is_nifty100.strip(),
        has_result.strip(), result_date_min.strip(), result_date_max.strip(),
        sectors.strip(), numeric_filters.strip(),
    ])

    # Cache only plain unfiltered pages
    if not has_any_filter:
        cache_key = f"all_dashboard:{limit}:{offset}"
        cached = await cache_service.get_json(cache_key)
        if cached is not None:
            return cached

    # Build dynamic WHERE conditions
    where_parts: list[str] = ["(su.is_active = TRUE OR su.is_active IS NULL)"]
    params: list = []
    post_numeric_filters: dict[str, dict[str, float]] = {}
    idx = 1  # asyncpg uses $1, $2, ...

    if search.strip():
        where_parts.append(f"(sdm.symbol ILIKE ${idx} OR su.company_name ILIKE ${idx})")
        params.append(f"%{search.strip()}%")
        idx += 1

    if symbol_type.strip():
        # treat "individual_securities" and "stock" as equivalent (both refer to equity)
        if symbol_type.strip() in ("individual_securities", "stock"):
            where_parts.append(f"su.symbol_type IN (${idx}, ${idx + 1})")
            params.extend(["individual_securities", "stock"])
            idx += 2
        else:
            where_parts.append(f"su.symbol_type = ${idx}")
            params.append(symbol_type.strip())
            idx += 1

    if is_nifty50.strip() == "true":
        where_parts.append("su.is_nifty50 = TRUE")

    if is_nifty100.strip() == "true":
        where_parts.append("su.is_nifty100 = TRUE")

    needs_has_result = has_result.strip()
    needs_date_range = result_date_min.strip() or result_date_max.strip()

    if has_result.strip() == "yes":
        where_parts.append("ev_filter.event_date IS NOT NULL")
    elif has_result.strip() == "no":
        where_parts.append("ev_filter.event_date IS NULL")

    if result_date_min.strip():
        where_parts.append(f"ev_date_filter.event_date >= ${idx}::date")
        try:
            params.append(date.fromisoformat(result_date_min.strip()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid result_date_min") from exc
        idx += 1

    if result_date_max.strip():
        where_parts.append(f"ev_date_filter.event_date <= ${idx}::date")
        try:
            params.append(date.fromisoformat(result_date_max.strip()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid result_date_max") from exc
        idx += 1

    if sectors.strip():
        sector_list = [s.strip() for s in sectors.split(",") if s.strip()]
        if sector_list:
            placeholders = ", ".join(f"${idx + i}" for i in range(len(sector_list)))
            where_parts.append(f"su.sector IN ({placeholders})")
            params.extend(sector_list)
            idx += len(sector_list)

    if numeric_filters.strip():
        try:
            nf = _json.loads(numeric_filters)
            for field, bounds in nf.items():
                col = FILTERABLE_NUMERIC.get(field)
                if col is None:
                    continue
                if isinstance(bounds, dict):
                    parsed_bounds: dict[str, float] = {}
                    if bounds.get("min") is not None:
                        parsed_bounds["min"] = float(bounds["min"])
                    if bounds.get("max") is not None:
                        parsed_bounds["max"] = float(bounds["max"])
                    if not parsed_bounds:
                        continue

                    if field in LIVE_OVERLAY_NUMERIC_FIELDS:
                        post_numeric_filters[field] = parsed_bounds
                        continue

                    if "min" in parsed_bounds:
                        where_parts.append(f"{col} >= ${idx}")
                        params.append(parsed_bounds["min"])
                        idx += 1
                    if "max" in parsed_bounds:
                        where_parts.append(f"{col} <= ${idx}")
                        params.append(parsed_bounds["max"])
                        idx += 1
        except Exception:
            pass

    where_clause = " AND ".join(where_parts)

    ev_filter_lateral = ""
    if needs_has_result:
        ev_filter_lateral = """
        LEFT JOIN LATERAL (
            SELECT event_date
            FROM events
            WHERE symbol = sdm.symbol
              AND event_type = 'RESULT'
              AND event_date >= CURRENT_DATE
              AND event_date <= CURRENT_DATE + INTERVAL '30 days'
            ORDER BY event_date ASC
            LIMIT 1
        ) ev_filter ON TRUE"""

    ev_date_filter_lateral = ""
    if needs_date_range:
        ev_date_filter_lateral = """
        LEFT JOIN LATERAL (
            SELECT event_date
            FROM events
            WHERE symbol = sdm.symbol
              AND event_type = 'RESULT'
              AND event_date >= CURRENT_DATE
            ORDER BY event_date ASC
            LIMIT 1
        ) ev_date_filter ON TRUE"""

    pagination_clause = ""
    if not post_numeric_filters:
        limit_param = idx
        offset_param = idx + 1
        pagination_clause = f"LIMIT ${limit_param} OFFSET ${offset_param}"

    full_query = f"""
        WITH latest_metrics AS (
            SELECT DISTINCT ON (symbol) *
            FROM symbol_daily_metrics
            ORDER BY symbol, trade_date DESC
        ),
        base AS (
            SELECT
                   sdm.symbol AS symbol_sort,
                   to_jsonb(sdm.*) ||
                   COALESCE(to_jsonb(sa.*), '{{}}'::jsonb) ||
                   jsonb_build_object(
                       'company_name', su.company_name,
                       'sector', su.sector,
                       'industry', su.industry,
                       'is_nifty50', su.is_nifty50,
                       'is_nifty100', su.is_nifty100,
                       'symbol_type', su.symbol_type,
                       'current_price', eq.close,
                       'rv_10', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_10 END,
                       'rv_20', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_20 END,
                       'rv_30', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_30 END,
                       'rv_60', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_60 END,
                       'rv_90', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_90 END,
                       'vrp', CASE WHEN sdm.vrp_signal_enabled THEN sdm.vrp END,
                       'max_fwdfct_3060', GREATEST(sdm.call_fwdfct_3060, sdm.put_fwdfct_3060),
                       'avg_vrp_4y', CASE WHEN sa.vrp_calculation_version >= 2 THEN sa.avg_vrp_4y END,
                       'vrp_win_rate', CASE WHEN sa.vrp_calculation_version >= 2 THEN sa.vrp_win_rate END,
                       'result_date', ev.event_date,
                       'result_event', CASE WHEN ev.event_date IS NOT NULL AND ev.event_date <= CURRENT_DATE + INTERVAL '30 days' THEN TRUE ELSE FALSE END,
                       'upcoming_events', COALESCE(evs.upcoming_events, '[]'::jsonb)
                   ) AS payload
            FROM latest_metrics sdm
            LEFT JOIN symbol_aggregates sa USING (symbol)
            LEFT JOIN symbol_universe su USING (symbol)
            LEFT JOIN LATERAL (
                SELECT close FROM equity_historical
                WHERE symbol = sdm.symbol ORDER BY trade_date DESC LIMIT 1
            ) eq ON TRUE
            LEFT JOIN LATERAL (
                SELECT event_date FROM events
                WHERE symbol = sdm.symbol
                  AND event_type = 'RESULT'
                  AND event_date >= CURRENT_DATE
                ORDER BY event_date ASC LIMIT 1
            ) ev ON TRUE
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'event_date', event_date,
                        'event_type', event_type,
                        'description', description,
                        'source', source
                    )
                    ORDER BY event_date ASC
                ) AS upcoming_events
                FROM (
                    SELECT event_date, event_type, description, source
                    FROM events
                    WHERE symbol = sdm.symbol
                      AND event_type = 'RESULT'
                      AND event_date >= CURRENT_DATE
                    ORDER BY event_date ASC
                    LIMIT 8
                ) upcoming
            ) evs ON TRUE
            {ev_filter_lateral}{ev_date_filter_lateral}
            WHERE {where_clause}
        )
        SELECT payload, COUNT(*) OVER() AS total
        FROM base
        ORDER BY symbol_sort
        {pagination_clause}
    """

    all_params = params if post_numeric_filters else params + [limit, offset]
    rows = await repo.pool.fetch(full_query, *all_params)

    payloads = [_json.loads(r["payload"]) for r in rows]
    if post_numeric_filters:
        live_payloads = await cache_service.get_live_symbols()
        live_by_symbol = {
            str(item.get("symbol") or "").upper(): item
            for item in live_payloads
            if item.get("symbol")
        }
        payloads = [
            _overlay_live_dashboard_payload(payload, live_by_symbol)
            for payload in payloads
        ]
        payloads = [
            payload
            for payload in payloads
            if _matches_numeric_filters(payload, post_numeric_filters)
        ]
        total = len(payloads)
        payloads = payloads[offset: offset + limit]
    else:
        total = rows[0]["total"] if rows else 0

    result = {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "data": payloads,
    }
    if not has_any_filter:
        await cache_service.set_json(f"all_dashboard:{limit}:{offset}", result)
    return result


@router.get("/symbol/{symbol}/volatility-cone")
async def symbol_volatility_cone(
    symbol: str,
    lookback_days: int = Query(default=504, ge=60, le=2000, description="Trading days of history for cone calculation"),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    """
    Volatility cone — historical RV percentile bands for each time window.
    Returns min/p10/p25/median/p75/p90/max for rv_10, rv_20, rv_30, rv_60, rv_90.
    For 30/60/90 tenors, current is the current IV reference plotted against those bands.
    """
    cache_key = f"vol_cone:v2:{symbol.upper()}:{lookback_days}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        result = cached
    else:
        rows = await repo.pool.fetch(
            """
            WITH history AS (
                SELECT rv_10, rv_20, rv_30, rv_60, rv_90
                FROM symbol_daily_metrics
                WHERE symbol = $1
                  AND rv_calculation_version >= 2
                  AND trade_date >= (SELECT MAX(trade_date) FROM symbol_daily_metrics WHERE symbol = $1)
                                    - ($2 * INTERVAL '1 day')
            )
            SELECT
                -- Percentile bands
                percentile_cont(0.00) WITHIN GROUP (ORDER BY rv_10) AS rv10_min,
                percentile_cont(0.10) WITHIN GROUP (ORDER BY rv_10) AS rv10_p10,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY rv_10) AS rv10_p25,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY rv_10) AS rv10_median,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY rv_10) AS rv10_p75,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY rv_10) AS rv10_p90,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY rv_10) AS rv10_max,

                percentile_cont(0.00) WITHIN GROUP (ORDER BY rv_20) AS rv20_min,
                percentile_cont(0.10) WITHIN GROUP (ORDER BY rv_20) AS rv20_p10,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY rv_20) AS rv20_p25,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY rv_20) AS rv20_median,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY rv_20) AS rv20_p75,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY rv_20) AS rv20_p90,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY rv_20) AS rv20_max,

                percentile_cont(0.00) WITHIN GROUP (ORDER BY rv_30) AS rv30_min,
                percentile_cont(0.10) WITHIN GROUP (ORDER BY rv_30) AS rv30_p10,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY rv_30) AS rv30_p25,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY rv_30) AS rv30_median,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY rv_30) AS rv30_p75,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY rv_30) AS rv30_p90,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY rv_30) AS rv30_max,

                percentile_cont(0.00) WITHIN GROUP (ORDER BY rv_60) AS rv60_min,
                percentile_cont(0.10) WITHIN GROUP (ORDER BY rv_60) AS rv60_p10,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY rv_60) AS rv60_p25,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY rv_60) AS rv60_median,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY rv_60) AS rv60_p75,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY rv_60) AS rv60_p90,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY rv_60) AS rv60_max,

                percentile_cont(0.00) WITHIN GROUP (ORDER BY rv_90) AS rv90_min,
                percentile_cont(0.10) WITHIN GROUP (ORDER BY rv_90) AS rv90_p10,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY rv_90) AS rv90_p25,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY rv_90) AS rv90_median,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY rv_90) AS rv90_p75,
                percentile_cont(0.90) WITHIN GROUP (ORDER BY rv_90) AS rv90_p90,
                percentile_cont(1.00) WITHIN GROUP (ORDER BY rv_90) AS rv90_max,

                COUNT(*) FILTER (WHERE rv_10 IS NOT NULL) AS sample_count
            FROM history
            """,
            symbol.upper(),
            lookback_days,
        )

        if not rows or rows[0]["sample_count"] == 0:
            raise HTTPException(status_code=404, detail="No volatility data for symbol")

        r = dict(rows[0])

        latest = await repo.pool.fetchrow(
            """
            SELECT rv_10::float, rv_20::float, rv_30::float, rv_60::float, rv_90::float,
                   iv_30::float, iv_60::float, iv_90::float,
                   dte_30, dte_60, dte_90,
                   expiry_30d, expiry_60d, expiry_90d,
                   trade_date
            FROM symbol_daily_metrics
            WHERE symbol = $1 AND rv_calculation_version >= 2
            ORDER BY trade_date DESC LIMIT 1
            """,
            symbol.upper(),
        )
        latest_reference = dict(latest) if latest else {}

        cone = {
            "rv_10": _volatility_cone_window(r, latest_reference, "rv10", 10),
            "rv_20": _volatility_cone_window(r, latest_reference, "rv20", 20),
            "rv_30": _volatility_cone_window(r, latest_reference, "rv30", 30),
            "rv_60": _volatility_cone_window(r, latest_reference, "rv60", 60),
            "rv_90": _volatility_cone_window(r, latest_reference, "rv90", 90),
        }
        result = {
            "symbol": symbol.upper(),
            "sample_count": r["sample_count"],
            "lookback_days": lookback_days,
            "as_of": _date_to_string(latest_reference.get("trade_date")),
            "current_reference_source": "symbol_daily_metrics",
            "x_axis_dtes": _volatility_cone_x_axis_dtes(cone),
            "cone": cone,
        }
        await cache_service.set_json(cache_key, result)

    live = await cache_service.get_live(symbol)
    return _overlay_live_volatility_cone(result, live)


def _volatility_cone_window(stats: dict[str, Any], latest: dict[str, Any], prefix: str, window_days: int) -> dict:
    rv_key = prefix.replace("rv", "rv_")
    iv_key = f"iv_{window_days}" if window_days in (30, 60, 90) else None
    dte_key = f"dte_{window_days}" if window_days in (30, 60, 90) else None
    expiry_key = f"expiry_{window_days}d" if window_days in (30, 60, 90) else None
    current_rv = _float_or_none(latest.get(rv_key))
    current_iv = _float_or_none(latest.get(iv_key)) if iv_key else None

    return {
        "min": _float_or_none(stats.get(f"{prefix}_min")),
        "p10": _float_or_none(stats.get(f"{prefix}_p10")),
        "p25": _float_or_none(stats.get(f"{prefix}_p25")),
        "median": _float_or_none(stats.get(f"{prefix}_median")),
        "p75": _float_or_none(stats.get(f"{prefix}_p75")),
        "p90": _float_or_none(stats.get(f"{prefix}_p90")),
        "max": _float_or_none(stats.get(f"{prefix}_max")),
        "current": current_iv if current_iv is not None else current_rv,
        "current_iv": current_iv,
        "current_rv": current_rv,
        "current_source": "iv" if current_iv is not None else "rv",
        "window_days": window_days,
        "target_dte": window_days,
        "dte": latest.get(dte_key) if dte_key and latest.get(dte_key) is not None else window_days,
        "expiry": _date_to_string(latest.get(expiry_key)) if expiry_key else None,
    }


def _volatility_cone_x_axis_dtes(cone: dict[str, dict]) -> dict[str, int | None]:
    return {key: window.get("dte") for key, window in cone.items()}


def _overlay_live_volatility_cone(result: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    if live.get("iv_term_structure_source") != "nse:option-chain-v3":
        return result

    cone = {key: dict(value) for key, value in result.get("cone", {}).items()}
    for tenor in (30, 60, 90):
        key = f"rv_{tenor}"
        if key not in cone:
            continue

        current_iv = _float_or_none(live.get(f"iv_{tenor}"))
        if current_iv is not None:
            cone[key]["current"] = current_iv
            cone[key]["current_iv"] = current_iv
            cone[key]["current_source"] = "iv"
            cone[key]["current_iv_source"] = live.get("iv_term_structure_source")

        dte = live.get(f"dte_{tenor}")
        if dte is not None:
            cone[key]["dte"] = dte

        expiry = live.get(f"expiry_{tenor}d")
        if expiry is not None:
            cone[key]["expiry"] = _date_to_string(expiry)

    return {
        **result,
        "current_reference_source": live.get("iv_term_structure_source"),
        "is_live": True,
        "snapshot_time": live.get("snapshot_time"),
        "x_axis_dtes": _volatility_cone_x_axis_dtes(cone),
        "cone": cone,
    }


@router.get("/symbol/{symbol}/term-structure")
async def symbol_term_structure(
    symbol: str,
    days: int = Query(default=252, ge=1, le=2000, description="Days of historical term structure to return"),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    """
    IV term structure — current snapshot + historical time-series.
    Returns the 30/60/90d IV with DTE and expiry dates for each trade date.
    """
    cache_key = f"term_structure:{symbol.upper()}:{days}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        result = cached
    else:
        rows = await repo.pool.fetch(
            """
            WITH ranked AS (
                SELECT trade_date,
                       iv_30::float, iv_60::float, iv_90::float,
                       call_iv_30::float, call_iv_60::float, call_iv_90::float,
                       put_iv_30::float, put_iv_60::float, put_iv_90::float,
                       dte_30, dte_60, dte_90,
                       expiry_30d, expiry_60d, expiry_90d,
                       fwdv_3060::float, fwdfct_3060::float,
                       call_fwdfct_3060::float, put_fwdfct_3060::float,
                       iv_slope_3060::float,
                       ROUND(
                           (PERCENT_RANK() OVER (ORDER BY fwdfct_3060 NULLS FIRST) * 100)::numeric, 2
                       )::float AS fwdfct_3060_percentile,
                       ROUND(
                           (PERCENT_RANK() OVER (ORDER BY iv_slope_3060 NULLS FIRST) * 100)::numeric, 2
                       )::float AS slope_percentile
                FROM symbol_daily_metrics
                WHERE symbol = $1
                ORDER BY trade_date DESC
                LIMIT $2
            )
            SELECT * FROM ranked
            ORDER BY trade_date DESC
            """,
            symbol.upper(),
            days,
        )

        series = [dict(r) for r in reversed(rows)]
        current = series[-1] if series else None

        result = {
            "symbol": symbol.upper(),
            "current": current,
            "history": series,
        }
        await cache_service.set_json(cache_key, result)

    live = await cache_service.get_live(symbol)
    return _overlay_live_term_structure(result, live)


def _overlay_live_term_structure(result: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    if live.get("iv_term_structure_source") != "nse:option-chain-v3":
        return result

    current = dict(result.get("current") or {})
    live_keys = [
        "iv_30",
        "iv_60",
        "iv_90",
        "call_iv_30",
        "call_iv_60",
        "call_iv_90",
        "put_iv_30",
        "put_iv_60",
        "put_iv_90",
        "dte_30",
        "dte_60",
        "dte_90",
        "expiry_30d",
        "expiry_60d",
        "expiry_90d",
        "fwdv_3060",
        "fwdfct_3060",
        "call_fwdfct_3060",
        "put_fwdfct_3060",
        "max_fwdfct_3060",
        "fev_30",
        "iv_slope_3060",
        "iv_term_structure_source",
        "forward_analytics_source",
        "iv_slope_3060_source",
        "live_iv_term_structure",
        "live_call_iv_term_structure",
        "live_put_iv_term_structure",
    ]
    for key in live_keys:
        if key in live:
            current[key] = live[key]
    current["is_live"] = True
    current["snapshot_time"] = live.get("snapshot_time")

    history = [dict(item) for item in result.get("history", [])]
    if history:
        history[-1] = {**history[-1], **current}
    else:
        history.append(current)

    return {
        **result,
        "current": current,
        "history": history,
    }


@router.get("/symbol/{symbol}/result-moves")
async def symbol_result_moves(
    symbol: str,
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    """
    Per-event result move history.
    Each row is one earnings/result event with: implied move (IV-based), actual underlying move,
    IV crush, and straddle P&L.
    """
    cache_key = f"result_moves:{symbol.upper()}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    rows = await repo.pool.fetch(
        """
        SELECT
            ev.event_date,
            ev.description,
            entry_sdm.iv_30::float          AS entry_iv30,
            exit_sdm.iv_30::float           AS exit_iv30,
            CASE WHEN entry_sdm.iv_30 > 0
                 THEN ((entry_sdm.iv_30 - exit_sdm.iv_30) / entry_sdm.iv_30)::float
            END                             AS iv_crush_pct,
            sp.total_entry::float           AS implied_straddle,
            sp.underlying_close::float      AS entry_underlying,
            CASE WHEN sp.underlying_close > 0
                 THEN (sp.total_entry / sp.underlying_close)::float
            END                             AS implied_move_pct,
            exit_eq.close::float            AS exit_underlying,
            CASE WHEN sp.underlying_close > 0
                 THEN ((exit_eq.close - sp.underlying_close) / sp.underlying_close)::float
            END                             AS actual_move_pct,
            (sp.total_entry - (ce_exit.close + pe_exit.close))::float AS straddle_pnl,
            CASE WHEN sp.total_entry > 0
                 THEN ((sp.total_entry - (ce_exit.close + pe_exit.close)) / sp.total_entry)::float
            END                             AS straddle_pnl_pct,
            sp.atm_strike::float,
            sp.expiry_date                  AS straddle_expiry
        FROM events ev
        JOIN LATERAL (
            SELECT trade_date FROM equity_historical eh
            WHERE eh.symbol = ev.symbol AND eh.trade_date < ev.event_date
            ORDER BY trade_date DESC LIMIT 1
        ) entry_day ON TRUE
        JOIN LATERAL (
            SELECT trade_date FROM equity_historical eh
            WHERE eh.symbol = ev.symbol AND eh.trade_date > ev.event_date
            ORDER BY trade_date LIMIT 1
        ) exit_day ON TRUE
        JOIN straddle_pnl sp
          ON sp.symbol = ev.symbol AND sp.trade_date = entry_day.trade_date AND sp.skip_reason IS NULL
        JOIN symbol_daily_metrics entry_sdm
          ON entry_sdm.symbol = ev.symbol AND entry_sdm.trade_date = entry_day.trade_date
        JOIN symbol_daily_metrics exit_sdm
          ON exit_sdm.symbol = ev.symbol AND exit_sdm.trade_date = exit_day.trade_date
        JOIN equity_historical exit_eq
          ON exit_eq.symbol = ev.symbol AND exit_eq.trade_date = exit_day.trade_date
        JOIN options_historical ce_exit
          ON ce_exit.symbol = ev.symbol AND ce_exit.trade_date = exit_day.trade_date
         AND ce_exit.expiry_date = sp.expiry_date AND ce_exit.strike_price = sp.atm_strike
         AND ce_exit.option_type = 'CE'
        JOIN options_historical pe_exit
          ON pe_exit.symbol = ev.symbol AND pe_exit.trade_date = exit_day.trade_date
         AND pe_exit.expiry_date = sp.expiry_date AND pe_exit.strike_price = sp.atm_strike
         AND pe_exit.option_type = 'PE'
        WHERE ev.symbol = $1 AND ev.event_type = 'RESULT'
          AND sp.underlying_close > 0
          AND ce_exit.close IS NOT NULL AND pe_exit.close IS NOT NULL
        ORDER BY ev.event_date DESC
        """,
        symbol.upper(),
    )

    events_data = [dict(r) for r in rows]

    summary = await repo.pool.fetchrow(
        """
        SELECT historical_iv_crush::float, implied_result_move::float,
               avg_result_move::float, max_result_move::float,
               avg_earnings_pnl::float, earnings_win_rate::float,
               max_earnings_profit::float, max_earnings_loss::float
        FROM symbol_aggregates WHERE symbol = $1
        """,
        symbol.upper(),
    )

    result = {
        "symbol": symbol.upper(),
        "summary": dict(summary) if summary else {},
        "events": events_data,
    }
    await cache_service.set_json(cache_key, result)
    return result


@router.get("/symbol/{symbol}/option-chain-history")
async def symbol_option_chain_history(
    symbol: str,
    trade_date: date = Query(..., description="Trade date YYYY-MM-DD"),
    expiry_date: date | None = Query(None, description="Filter to one expiry, or omit for nearest"),
    repo: MarketRepository = Depends(repository),
) -> dict:
    """
    Historical option chain for a specific trade date — used for IV smile / skew charts.
    Returns strike, CE IV, PE IV, CE delta, PE delta, CE OI, PE OI.
    """
    if expiry_date is None:
        expiry_date = await repo.pool.fetchval(
            """
            SELECT MIN(expiry_date) FROM options_historical
            WHERE symbol = $1 AND trade_date = $2 AND expiry_date > $2
            """,
            symbol.upper(),
            trade_date,
        )
        if expiry_date is None:
            raise HTTPException(status_code=404, detail="No option data for this date")

    rows = await repo.pool.fetch(
        """
        SELECT
            strike_price::float,
            MAX(CASE WHEN option_type='CE' THEN iv::float END)            AS ce_iv,
            MAX(CASE WHEN option_type='PE' THEN iv::float END)            AS pe_iv,
            MAX(CASE WHEN option_type='CE' THEN delta::float END)         AS ce_delta,
            MAX(CASE WHEN option_type='PE' THEN delta::float END)         AS pe_delta,
            MAX(CASE WHEN option_type='CE' THEN open_interest END)        AS ce_oi,
            MAX(CASE WHEN option_type='PE' THEN open_interest END)        AS pe_oi,
            MAX(CASE WHEN option_type='CE' THEN close::float END)         AS ce_ltp,
            MAX(CASE WHEN option_type='PE' THEN close::float END)         AS pe_ltp,
            MAX(CASE WHEN option_type='CE' THEN num_contracts END)        AS ce_volume,
            MAX(CASE WHEN option_type='PE' THEN num_contracts END)        AS pe_volume,
            BOOL_OR(is_atm)                                               AS is_atm
        FROM options_historical
        WHERE symbol = $1 AND trade_date = $2 AND expiry_date = $3
          AND iv IS NOT NULL
        GROUP BY strike_price
        ORDER BY strike_price
        """,
        symbol.upper(),
        trade_date,
        expiry_date,
    )

    underlying = await repo.pool.fetchval(
        "SELECT close::float FROM equity_historical WHERE symbol=$1 AND trade_date=$2",
        symbol.upper(),
        trade_date,
    )

    return {
        "symbol": symbol.upper(),
        "trade_date": str(trade_date),
        "expiry_date": str(expiry_date),
        "underlying_close": underlying,
        "strikes": [dict(r) for r in rows],
    }


@router.get("/symbols")
async def symbols(
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[str]:
    cached = await cache_service.get_json("symbols:list")
    if cached is not None:
        return cached
    result = await repo.active_symbols()
    await cache_service.set_json("symbols:list", result)
    return result


@router.get("/symbol/{symbol}")
async def symbol_dashboard(
    symbol: str,
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    cached = await cache_service.get_dashboard(symbol)
    if cached is None:
        cached = await repo.dashboard_row(symbol)
        if cached is None:
            raise HTTPException(status_code=404, detail="symbol not found")
        await cache_service.set_dashboard(symbol, cached)
    live = await cache_service.get_live(symbol)
    return {**cached, **live}


@router.get("/symbol/{symbol}/history")
async def symbol_history(
    symbol: str,
    days: int = Query(default=365, ge=1, le=2000),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    cache_key = f"history:{symbol.upper()}:{days}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    result = await repo.history(symbol, days)
    await cache_service.set_json(cache_key, result)
    return result


@router.get("/symbol/{symbol}/pnl")
async def symbol_pnl(
    symbol: str,
    days: int = Query(default=365, ge=1, le=2000),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    cache_key = f"pnl:{symbol.upper()}:{days}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    result = await repo.straddle_history(symbol, days)
    await cache_service.set_json(cache_key, result)
    return result


@router.get("/live/{symbol}")
async def live(
    symbol: str,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    symbol = symbol.upper()
    payload = await cache_service.get_live(symbol)
    if not payload:
        result = await fetch_and_store_live_quotes(settings, repo, cache_service.redis, [symbol])
        payload = await cache_service.get_live(symbol)
        if not payload:
            raise HTTPException(status_code=404, detail=result)
    return payload


@router.get("/live")
async def live_symbols(
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    payload = await cache_service.get_live_symbols()
    if payload:
        return payload
    await fetch_and_store_live_quotes(settings, repo, cache_service.redis)
    return await cache_service.get_live_symbols()


@router.get("/live/{symbol}/option-chain")
async def live_option_chain(
    symbol: str,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    cached = await cache_service.get_live(f"chain:{symbol}")
    if cached:
        return cached
    redis = Redis.from_url(settings.redis_url)
    try:
        result = await fetch_and_store_live_snapshots(settings, repo, redis, [symbol.upper()])
        payload = await CacheService(redis).get_live(f"chain:{symbol}")
        if payload:
            return payload
        raise HTTPException(status_code=404, detail=result)
    finally:
        await redis.aclose()


@router.post("/admin/live-quotes")
async def trigger_live_quotes(
    symbols: str | None = None,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
) -> dict:
    redis = Redis.from_url(settings.redis_url)
    try:
        symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
        return await fetch_and_store_live_quotes(settings, repo, redis, symbol_list)
    except Exception as exc:
        await repo.log_error(
            "api_live_quotes",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            source=settings.live_quote_provider,
        )
        raise
    finally:
        await redis.aclose()


@router.post("/admin/live-snapshot")
async def trigger_live_snapshot(
    symbols: str | None = None,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
) -> dict:
    redis = Redis.from_url(settings.redis_url)
    try:
        symbol_list = [item.strip().upper() for item in symbols.split(",")] if symbols else None
        return await fetch_and_store_live_snapshots(settings, repo, redis, symbol_list)
    except Exception as exc:
        await repo.log_error(
            "api_live_snapshot",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbols},
            source=settings.live_option_chain_provider,
        )
        raise
    finally:
        await redis.aclose()


@router.post("/admin/trigger-pipeline")
async def trigger_pipeline(
    trade_date: date,
    symbols: str | None = None,
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
) -> dict:
    pipeline = Pipeline(
        settings=settings,
        repository=repo,
        bhavcopy_source=build_bhavcopy_source(settings),
        rates=IndiaRiskFreeRateClient(default_rate=settings.default_risk_free_rate),
        corporate_actions_source=build_corporate_actions_source(settings),
    )
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    try:
        return await pipeline.run_for_date(trade_date, symbol_list, finalize=True)
    except Exception as exc:
        await repo.log_error(
            "api_trigger_pipeline",
            type(exc).__name__,
            {"message": str(exc), "repr": repr(exc), "symbols": symbol_list},
            trade_date=trade_date,
            source="admin_api",
        )
        raise
