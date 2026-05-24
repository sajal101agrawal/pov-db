from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.pool import get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.services.cache import CacheService
from app.services.factory import build_bhavcopy_source
from app.services.live import fetch_and_store_live_quotes, fetch_and_store_live_snapshots
from app.sources.rates import IndiaRiskFreeRateClient


router = APIRouter()


async def repository() -> MarketRepository:
    return MarketRepository(await get_pool())


async def cache(settings: Settings = Depends(get_settings)) -> CacheService:
    return CacheService(Redis.from_url(settings.redis_url))


@router.get("/health")
async def health(repo: MarketRepository = Depends(repository)) -> dict:
    latest = await repo.latest_trade_date()
    return {"ok": True, "latest_trade_date": latest}


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
    "vrp":               "sdm.vrp",
    "iv_30":             "sdm.iv_30",
    "iv_60":             "sdm.iv_60",
    "iv_90":             "sdm.iv_90",
    "iv_30_percentile":  "sdm.iv_30_percentile",
    "iv_60_percentile":  "sdm.iv_60_percentile",
    "iv_90_percentile":  "sdm.iv_90_percentile",
    "fwdv_3060":         "sdm.fwdv_3060",
    "fwdfct_3060":       "sdm.fwdfct_3060",
    "iv_slope_3060":     "sdm.iv_slope_3060",
    "rv_10":             "sdm.rv_10",
    "rv_20":             "sdm.rv_20",
    "rv_30":             "sdm.rv_30",
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
    "nearest_pe_iv":     "sdm.nearest_pe_iv",
    "iv30_rv30_ratio":   "sdm.iv30_rv30_ratio",
    "iv30_fev30_ratio":  "sdm.iv30_fev30_ratio",
    "avg_option_volume": "sdm.avg_option_volume",
    "avg_straddle_pnl":  "sa.avg_straddle_pnl",
    "vrp_win_rate":      "sa.vrp_win_rate",
    "avg_vrp_4y":        "sa.avg_vrp_4y",
    "max_loss":          "sa.max_loss",
    "max_profit":        "sa.max_profit",
    "current_price":     "eq.close",
}


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

    needs_ev_filter = has_result.strip() or result_date_min.strip() or result_date_max.strip()

    if has_result.strip() == "yes":
        where_parts.append("ev_filter.event_date IS NOT NULL")
    elif has_result.strip() == "no":
        where_parts.append("ev_filter.event_date IS NULL")

    if result_date_min.strip():
        where_parts.append(f"ev_filter.event_date >= ${idx}::date")
        try:
            params.append(date.fromisoformat(result_date_min.strip()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid result_date_min") from exc
        idx += 1

    if result_date_max.strip():
        where_parts.append(f"ev_filter.event_date <= ${idx}::date")
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
                    if bounds.get("min") is not None:
                        where_parts.append(f"{col} >= ${idx}")
                        params.append(float(bounds["min"]))
                        idx += 1
                    if bounds.get("max") is not None:
                        where_parts.append(f"{col} <= ${idx}")
                        params.append(float(bounds["max"]))
                        idx += 1
        except Exception:
            pass

    where_clause = " AND ".join(where_parts)

    ev_filter_lateral = ""
    if needs_ev_filter:
        ev_filter_lateral = """
        LEFT JOIN LATERAL (
            SELECT event_date
            FROM events
            WHERE symbol = sdm.symbol
              AND event_type = 'RESULT'
              AND event_date >= CURRENT_DATE
            ORDER BY event_date ASC
            LIMIT 1
        ) ev_filter ON TRUE"""

    limit_param = idx
    offset_param = idx + 1

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
                       'result_date', ev.event_date,
                       'result_event', CASE WHEN ev.event_date IS NOT NULL THEN TRUE ELSE FALSE END,
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
            {ev_filter_lateral}
            WHERE {where_clause}
        )
        SELECT payload, COUNT(*) OVER() AS total
        FROM base
        ORDER BY symbol_sort
        LIMIT ${limit_param} OFFSET ${offset_param}
    """

    all_params = params + [limit, offset]
    rows = await repo.pool.fetch(full_query, *all_params)

    total = rows[0]["total"] if rows else 0
    result = {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "data": [_json.loads(r["payload"]) for r in rows],
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
    Returns min/p10/p25/median/p75/p90/max for rv_10, rv_20, rv_30, rv_60, rv_90,
    plus the current (latest) realized vol for each window.
    """
    cache_key = f"vol_cone:{symbol.upper()}:{lookback_days}"
    cached = await cache_service.get_json(cache_key)
    if cached is not None:
        return cached
    rows = await repo.pool.fetch(
        """
        WITH history AS (
            SELECT rv_10, rv_20, rv_30, rv_60, rv_90
            FROM symbol_daily_metrics
            WHERE symbol = $1
              AND trade_date >= (SELECT MAX(trade_date) FROM symbol_daily_metrics WHERE symbol = $1)
                                - ($2 * INTERVAL '1 day')
        ),
        latest AS (
            SELECT rv_10, rv_20, rv_30, rv_60, rv_90
            FROM symbol_daily_metrics
            WHERE symbol = $1
            ORDER BY trade_date DESC
            LIMIT 1
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
        SELECT rv_10::float, rv_20::float, rv_30::float, rv_60::float, rv_90::float, trade_date
        FROM symbol_daily_metrics WHERE symbol = $1 ORDER BY trade_date DESC LIMIT 1
        """,
        symbol.upper(),
    )

    def _window(prefix: str) -> dict:
        return {
            "min": float(r[f"{prefix}_min"]) if r[f"{prefix}_min"] is not None else None,
            "p10": float(r[f"{prefix}_p10"]) if r[f"{prefix}_p10"] is not None else None,
            "p25": float(r[f"{prefix}_p25"]) if r[f"{prefix}_p25"] is not None else None,
            "median": float(r[f"{prefix}_median"]) if r[f"{prefix}_median"] is not None else None,
            "p75": float(r[f"{prefix}_p75"]) if r[f"{prefix}_p75"] is not None else None,
            "p90": float(r[f"{prefix}_p90"]) if r[f"{prefix}_p90"] is not None else None,
            "max": float(r[f"{prefix}_max"]) if r[f"{prefix}_max"] is not None else None,
            "current": float(latest[prefix.replace("rv", "rv_")]) if latest and latest[prefix.replace("rv", "rv_")] is not None else None,
        }

    result = {
        "symbol": symbol.upper(),
        "sample_count": r["sample_count"],
        "lookback_days": lookback_days,
        "as_of": str(latest["trade_date"]) if latest else None,
        "cone": {
            "rv_10": _window("rv10"),
            "rv_20": _window("rv20"),
            "rv_30": _window("rv30"),
            "rv_60": _window("rv60"),
            "rv_90": _window("rv90"),
        },
    }
    await cache_service.set_json(cache_key, result)
    return result


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
        return cached
    rows = await repo.pool.fetch(
        """
        WITH ranked AS (
            SELECT trade_date,
                   iv_30::float, iv_60::float, iv_90::float,
                   dte_30, dte_60, dte_90,
                   expiry_30d, expiry_60d, expiry_90d,
                   fwdv_3060::float, fwdfct_3060::float, iv_slope_3060::float,
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
    return result


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
async def live(symbol: str, cache_service: CacheService = Depends(cache)) -> dict:
    payload = await cache_service.get_live(symbol)
    if not payload:
        raise HTTPException(status_code=404, detail="live data not available")
    return payload


@router.get("/live")
async def live_symbols(cache_service: CacheService = Depends(cache)) -> list[dict]:
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
            source="dhan",
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
            source="dhan",
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
