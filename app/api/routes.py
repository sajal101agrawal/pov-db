from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime
from html import escape
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.db.pool import get_pool
from app.db.repository import MarketRepository
from app.etl.pipeline import Pipeline
from app.services.cache import CacheService
from app.services.factory import build_bhavcopy_source, build_corporate_actions_source
from app.services.live import (
    IST,
    _dhan_access_token,
    _kite_access_token,
    fetch_and_store_live_quotes,
    fetch_and_store_live_snapshots,
    generate_kite_access_token,
    in_market_window,
    kite_login_url_for_settings,
)
from app.sources.dhan import (
    DhanOptionChainClient,
    normalize_market_quotes as normalize_dhan_market_quotes,
)
from app.sources.kite import KiteConnectClient, quote_last_price as kite_quote_last_price
from app.sources.nse_option_chain import NSEOptionChainClient
from app.sources.rates import IndiaRiskFreeRateClient
from app.sources.yahoo import YahooFinanceClient


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


@router.get("/system-health")
async def system_health(
    symbol: str = Query(default="RELIANCE", description="Symbol to probe live providers with"),
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    """On-demand browser-friendly health check for DB, Redis, and live data providers."""
    probe_symbol = (symbol or "RELIANCE").strip().upper()
    checks = dict(
        await asyncio.gather(
            _run_health_check("database", _check_database(repo)),
            _run_health_check("redis", _check_redis(cache_service)),
            _run_health_check(
                "kite",
                _check_kite(settings, repo, cache_service, probe_symbol),
                required=bool(_provider_roles(settings, "kite")),
            ),
            _run_health_check(
                "nse_option_chain",
                _check_nse_option_chain(settings, probe_symbol),
                required=bool(_provider_roles(settings, "nse")),
            ),
            _run_health_check(
                "yahoo",
                _check_yahoo(repo, settings, probe_symbol),
                required=bool(_provider_roles(settings, "yahoo")),
            ),
            _run_health_check(
                "dhan",
                _check_dhan(settings, cache_service, probe_symbol),
                required=bool(_provider_roles(settings, "dhan")),
            ),
            _run_health_check("live_state", _check_live_state(settings, repo, cache_service, probe_symbol)),
        )
    )
    status = _overall_health_status(checks)
    current_sources = _current_source_config(settings)
    return {
        "ok": status != "fail",
        "status": status,
        "generated_at": datetime.now(IST).isoformat(),
        "symbol": probe_symbol,
        "current_sources": current_sources,
        "config": {
            "app_env": settings.app_env,
            **current_sources,
            "live_symbols": settings.live_symbols,
            "live_cache_ttl_seconds": settings.live_cache_ttl_seconds,
            "live_poll_interval_seconds": settings.live_poll_interval_seconds,
            "market_window_ist": {
                "start": settings.live_market_start_ist,
                "end": settings.live_market_end_ist,
                "in_window_now": in_market_window(settings),
            },
        },
        "checks": checks,
    }


async def _run_health_check(
    name: str,
    check: Any,
    *,
    required: bool = True,
) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    try:
        result = await check
        payload = result if isinstance(result, dict) else {"status": "ok", "details": result}
    except Exception as exc:  # noqa: BLE001 - health endpoint must report failures
        payload = {"status": "fail", "error": _health_error(exc)}
    payload.setdefault("status", "ok")
    payload.setdefault("required", required)
    payload["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return name, payload


def _overall_health_status(checks: dict[str, dict[str, Any]]) -> str:
    has_warning = False
    for item in checks.values():
        status = str(item.get("status") or "ok")
        if status == "fail":
            if item.get("required", True):
                return "fail"
            has_warning = True
        elif status == "warn":
            has_warning = True
    if has_warning:
        return "warn"
    return "ok"


def _health_error(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc)[:500],
    }
    if isinstance(exc, httpx.HTTPStatusError):
        payload["status_code"] = exc.response.status_code
        payload["reason"] = exc.response.reason_phrase
    return payload


async def _check_database(repo: MarketRepository) -> dict[str, Any]:
    db_now = await repo.pool.fetchval("SELECT NOW()")
    latest_trade_date = await repo.latest_trade_date()
    live_count = await repo.pool.fetchval("SELECT COUNT(*) FROM live_symbol_metrics")
    latest_live_snapshot = await repo.pool.fetchval(
        "SELECT MAX(snapshot_time) FROM live_symbol_metrics"
    )
    return {
        "status": "ok",
        "database_time": db_now.isoformat() if db_now else None,
        "latest_trade_date": latest_trade_date.isoformat() if latest_trade_date else None,
        "live_symbol_metrics_rows": int(live_count or 0),
        "latest_live_snapshot": latest_live_snapshot.isoformat() if latest_live_snapshot else None,
    }


async def _check_redis(cache_service: CacheService) -> dict[str, Any]:
    pong = await cache_service.redis.ping()
    return {"status": "ok" if pong else "fail", "ping": bool(pong)}


async def _check_kite(
    settings: Settings,
    repo: MarketRepository,
    cache_service: CacheService,
    symbol: str,
) -> dict[str, Any]:
    active_for = _provider_roles(settings, "kite")
    if not settings.kite_api_key:
        return {
            "status": "fail" if active_for else "disabled",
            "configured": False,
            "active_for": active_for,
        }
    stored = await repo.broker_access_token("kite")
    token_details = _broker_token_details(stored)
    access_token = await _kite_access_token(settings, repo, cache_service.redis)
    client = KiteConnectClient(
        settings.kite_api_key,
        access_token,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instrument = f"NSE:{symbol}"
    payload = await client.quote([instrument])
    last_price = kite_quote_last_price(payload, instrument)
    data = (payload.get("data") or {}).get(instrument) or {}
    return {
        "status": "ok" if last_price is not None else "warn",
        "configured": True,
        "active_for": active_for,
        "api_key": _mask_value(settings.kite_api_key),
        "stored_token": token_details,
        "probe": {
            "instrument": instrument,
            "last_price": last_price,
            "volume": data.get("volume"),
            "oi": data.get("oi"),
        },
    }


async def _check_nse_option_chain(settings: Settings, symbol: str) -> dict[str, Any]:
    active_for = _provider_roles(settings, "nse")
    client = NSEOptionChainClient(
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
        settings.live_option_summary_concurrency,
        settings.live_option_summary_min_interval_seconds,
    )
    chain = await client.fetch_chain(symbol)
    if not chain:
        return {
            "status": "fail",
            "active_for": active_for,
            "message": "NSE option chain returned no payload",
        }
    return {
        "status": "ok" if chain.get("strike_count") else "warn",
        "active_for": active_for,
        "source": chain.get("source"),
        "expiry": chain.get("expiry"),
        "underlying_last_price": chain.get("underlying_last_price"),
        "strike_count": chain.get("strike_count"),
        "timestamp": chain.get("nse_option_chain_timestamp"),
    }


async def _check_yahoo(
    repo: MarketRepository,
    settings: Settings,
    symbol: str,
) -> dict[str, Any]:
    active_for = _provider_roles(settings, "yahoo")
    yahoo_symbols = await repo.yahoo_symbols_for([symbol])
    client = YahooFinanceClient(
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    rows = await client.fetch_live_quotes([symbol], yahoo_symbols)
    quote = rows.get(symbol)
    if not quote:
        return {
            "status": "fail",
            "active_for": active_for,
            "message": "Yahoo returned no live quote",
        }
    return {
        "status": "ok",
        "active_for": active_for,
        "provider_symbol": quote.get("provider_symbol"),
        "current_price": quote.get("current_price"),
        "market_state": quote.get("market_state"),
        "regular_market_time": quote.get("regular_market_time"),
    }


async def _check_dhan(
    settings: Settings,
    cache_service: CacheService,
    symbol: str,
) -> dict[str, Any]:
    active_for = _provider_roles(settings, "dhan")
    if not settings.dhan_client_id:
        return {
            "status": "fail" if active_for else "disabled",
            "configured": False,
            "active_for": active_for,
        }
    access_token = await _dhan_access_token(settings, cache_service.redis)
    client = DhanOptionChainClient(
        settings.dhan_client_id,
        access_token,
        settings.live_market_quote_min_interval_seconds,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instrument_map = await client.instrument_map({symbol})
    instrument = instrument_map.get(symbol)
    if not instrument:
        return {
            "status": "warn",
            "configured": True,
            "active_for": active_for,
            "message": "No Dhan instrument mapping",
        }
    segment = instrument["underlying_seg"]
    security_id = instrument["underlying_scrip"]
    raw = await client.market_quote({segment: [security_id]})
    quotes = normalize_dhan_market_quotes({(segment, security_id): symbol}, raw)
    quote = quotes.get(symbol)
    if not quote:
        return {
            "status": "fail",
            "configured": True,
            "active_for": active_for,
            "message": "Dhan returned no quote",
        }
    return {
        "status": "ok",
        "configured": True,
        "active_for": active_for,
        "instrument_source": instrument.get("source"),
        "segment": segment,
        "security_id": security_id,
        "current_price": quote.get("current_price"),
    }


async def _check_live_state(
    settings: Settings,
    repo: MarketRepository,
    cache_service: CacheService,
    symbol: str,
) -> dict[str, Any]:
    cached = await cache_service.get_live(symbol)
    stored = (await repo.latest_live_metrics([symbol])).get(symbol)
    live = cached or stored
    redis_ttl = await cache_service.redis.ttl(f"live:{symbol.upper()}")
    if not live:
        return {"status": "warn", "message": "No live payload in Redis or database"}
    age_seconds = _snapshot_age_seconds(live.get("snapshot_time"))
    stale_after_seconds = max(
        int(settings.live_cache_ttl_seconds),
        int(settings.live_poll_interval_seconds) * 2,
    )
    off_market_stale_after_seconds = 24 * 60 * 60
    is_market_open = in_market_window(settings)
    status = "ok"
    message = None
    if age_seconds is None:
        status = "warn"
        message = "Live payload snapshot_time is missing or invalid"
    elif is_market_open and age_seconds > stale_after_seconds:
        status = "warn"
        message = "Live payload is stale for the configured market window"
    elif not is_market_open and age_seconds > off_market_stale_after_seconds:
        status = "warn"
        message = "Live payload is older than 24 hours"
    return {
        "status": status,
        "message": message,
        "source": "redis" if cached else "database",
        "redis_key_present": bool(cached),
        "redis_ttl_seconds": redis_ttl,
        "database_row_present": bool(stored),
        "snapshot_time": live.get("snapshot_time"),
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "off_market_stale_after_seconds": off_market_stale_after_seconds,
        "market_window_open_now": is_market_open,
        "quote_provider": live.get("quote_provider") or live.get("provider"),
        "option_provider": live.get("live_option_provider"),
        "forward_analytics_source": live.get("forward_analytics_source"),
        "current_price": live.get("current_price"),
        "iv_30": live.get("iv_30"),
        "dte_30": live.get("dte_30"),
        "dte_60": live.get("dte_60"),
    }


def _current_source_config(settings: Settings) -> dict[str, str]:
    return {
        "live_quote_provider": settings.live_quote_provider.lower().strip(),
        "live_option_summary_provider": settings.live_option_summary_provider.lower().strip(),
        "live_option_chain_provider": settings.live_option_chain_provider.lower().strip(),
    }


def _provider_roles(settings: Settings, provider: str) -> list[str]:
    provider = provider.lower().strip()
    sources = _current_source_config(settings)
    roles = []
    if sources["live_quote_provider"] == provider:
        roles.append("quote")
    if sources["live_option_summary_provider"] == provider:
        roles.append("option_summary")
    if sources["live_option_chain_provider"] == provider:
        roles.append("option_chain")
    return roles


def _snapshot_age_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return round((datetime.now(IST) - parsed.astimezone(IST)).total_seconds(), 2)


def _broker_token_details(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {"present": False}
    expires_at = payload.get("expires_at")
    usable = False
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=IST)
            usable = expiry > datetime.now(expiry.tzinfo)
        except ValueError:
            usable = False
    return {
        "present": True,
        "expires_at": expires_at,
        "updated_at": payload.get("updated_at"),
        "usable_now": usable,
    }


def _mask_value(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return None
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}...{value[-keep:]}"


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
    "call_fwdfct_3060_percentile": "sdm.call_fwdfct_3060_percentile",
    "put_fwdfct_3060_percentile": "sdm.put_fwdfct_3060_percentile",
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
    "call_fwdfct_3060_percentile",
    "put_fwdfct_3060_percentile",
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


async def _live_payloads_by_symbol(
    cache_service: CacheService,
    repo: MarketRepository,
    symbols: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    clean_symbols = (
        [symbol.upper() for symbol in symbols if symbol]
        if symbols is not None
        else None
    )
    live_by_symbol = await repo.latest_live_metrics(clean_symbols)

    if clean_symbols is None:
        redis_payloads = await cache_service.get_live_symbols()
    else:
        redis_payloads = []
        for symbol in clean_symbols:
            live = await cache_service.get_live(symbol)
            if live:
                redis_payloads.append(live)

    for item in redis_payloads:
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            live_by_symbol[symbol] = item
    return live_by_symbol


async def _latest_live_payload(
    symbol: str,
    cache_service: CacheService,
    repo: MarketRepository,
) -> dict[str, Any]:
    symbol = symbol.upper()
    live = await cache_service.get_live(symbol)
    if live:
        return live
    return (await repo.latest_live_metrics([symbol])).get(symbol, {})


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
        live_by_symbol = await _live_payloads_by_symbol(cache_service, repo)
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
        live_by_symbol = await _live_payloads_by_symbol(
            cache_service,
            repo,
            [str(payload.get("symbol") or "").upper() for payload in payloads if payload.get("symbol")],
        )
        payloads = [
            _overlay_live_dashboard_payload(payload, live_by_symbol)
            for payload in payloads
        ]

    result = {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "data": payloads,
    }
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

    live = await _latest_live_payload(symbol, cache_service, repo)
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
    if not live.get("iv_term_structure_source"):
        return result

    cone = {key: dict(value) for key, value in result.get("cone", {}).items()}
    live_tenors = 0
    for tenor in (30, 60, 90):
        key = f"rv_{tenor}"
        if key not in cone:
            continue

        current_iv = _float_or_none(live.get(f"iv_{tenor}"))
        if current_iv is None:
            continue

        live_tenors += 1
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

    if not live_tenors:
        return result

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
                       COALESCE(
                           call_fwdfct_3060_percentile::float,
                           ROUND(
                               (PERCENT_RANK() OVER (ORDER BY call_fwdfct_3060 NULLS FIRST) * 100)::numeric, 2
                           )::float
                       ) AS call_fwdfct_3060_percentile,
                       COALESCE(
                           put_fwdfct_3060_percentile::float,
                           ROUND(
                               (PERCENT_RANK() OVER (ORDER BY put_fwdfct_3060 NULLS FIRST) * 100)::numeric, 2
                           )::float
                       ) AS put_fwdfct_3060_percentile,
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

    live = await _latest_live_payload(symbol, cache_service, repo)
    return _overlay_live_term_structure(result, live)


def _overlay_live_term_structure(result: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    if not live.get("iv_term_structure_source"):
        return result

    current = dict(result.get("current") or {})
    history = [dict(item) for item in result.get("history", [])]
    live_keys = [
        "current_price",
        "last_price",
        "provider",
        "provider_symbol",
        "quote_provider",
        "quote_provider_symbol",
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
        "call_fwdfct_3060_percentile",
        "put_fwdfct_3060_percentile",
        "fev_30",
        "iv_slope_3060",
        "iv_term_structure_source",
        "forward_analytics_source",
        "iv_slope_3060_source",
        "live_iv_term_structure",
        "live_call_iv_term_structure",
        "live_put_iv_term_structure",
        "live_raw_iv_term_structure",
        "live_raw_call_iv_term_structure",
        "live_raw_put_iv_term_structure",
    ]
    for key in live_keys:
        if key in live:
            current[key] = live[key]
    _refresh_current_forward_factor_percentiles(history, current)
    current["is_live"] = True
    current["snapshot_time"] = live.get("snapshot_time")

    if history:
        history[-1] = {**history[-1], **current}
    else:
        history.append(current)

    return {
        **result,
        "current": current,
        "history": history,
    }


def _overlay_live_history(history: list[dict], live: dict[str, Any]) -> list[dict]:
    if not live:
        return history
    live_keys = [
        "current_price",
        "last_price",
        "provider",
        "provider_symbol",
        "quote_provider",
        "quote_provider_symbol",
        "iv_30",
        "iv_60",
        "iv_90",
        "call_iv_30",
        "call_iv_60",
        "call_iv_90",
        "put_iv_30",
        "put_iv_60",
        "put_iv_90",
        "rv_10",
        "rv_20",
        "rv_30",
        "rv_60",
        "rv_90",
        "vrp",
        "fwdv_3060",
        "fwdfct_3060",
        "call_fwdfct_3060",
        "put_fwdfct_3060",
        "max_fwdfct_3060",
        "call_fwdfct_3060_percentile",
        "put_fwdfct_3060_percentile",
        "fev_30",
        "iv_slope_3060",
        "iv30_rv30_ratio",
        "iv30_fev30_ratio",
        "avg_option_volume",
        "avg_option_volume_source",
        "avg_option_volume_kind",
        "live_option_provider",
        "live_option_volume",
        "live_option_volume_source",
        "live_option_volume_kind",
        "live_option_expiry",
        "live_option_expiry_date",
        "live_option_strike_count",
        "live_option_underlying",
        "live_atm_strike",
        "live_atm_iv",
        "live_atm_iv_source",
        "live_atm_call_iv",
        "live_atm_put_iv",
        "live_atm_call_ltp",
        "live_atm_put_ltp",
        "live_atm_call_volume",
        "live_atm_put_volume",
        "live_atm_option_volume",
        "live_iv_terms",
        "live_iv_term_count",
        "live_iv_term_structure",
        "live_call_iv_term_structure",
        "live_put_iv_term_structure",
        "live_raw_iv_term_structure",
        "live_raw_call_iv_term_structure",
        "live_raw_put_iv_term_structure",
        "dte_30",
        "dte_60",
        "dte_90",
        "expiry_30d",
        "expiry_60d",
        "expiry_90d",
        "iv_term_structure_source",
        "forward_analytics_source",
        "iv_slope_3060_source",
    ]
    current = {key: live[key] for key in live_keys if key in live}
    if not current:
        return history
    snapshot_time = live.get("snapshot_time")
    trade_date = _date_from_snapshot(snapshot_time)
    if trade_date is None:
        trade_date = datetime.now().date().isoformat()
    current.update(
        {
            "trade_date": trade_date,
            "is_live": True,
            "snapshot_time": snapshot_time,
        }
    )

    result = [dict(item) for item in history]
    _refresh_current_forward_factor_percentiles(result, current)
    if result and _date_to_string(result[-1].get("trade_date")) == trade_date:
        result[-1] = {**result[-1], **current}
    else:
        result.append(current)
    return result


def _refresh_current_forward_factor_percentiles(
    history: list[dict[str, Any]],
    current: dict[str, Any],
) -> None:
    fields = {
        "fwdfct_3060": "fwdfct_3060_percentile",
        "call_fwdfct_3060": "call_fwdfct_3060_percentile",
        "put_fwdfct_3060": "put_fwdfct_3060_percentile",
    }
    for value_field, percentile_field in fields.items():
        percentile = _percent_rank_current(history, value_field, current.get(value_field))
        if percentile is not None:
            current[percentile_field] = percentile


async def _refresh_live_payload_forward_percentiles(
    symbol: str,
    payload: dict[str, Any],
    repo: MarketRepository,
) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    if not symbol or not payload or not payload.get("snapshot_time"):
        return payload
    if not any(
        payload.get(field) is not None
        for field in ("fwdfct_3060", "call_fwdfct_3060", "put_fwdfct_3060")
    ):
        return payload
    history = await repo.history(symbol, 252)
    current = dict(payload)
    _refresh_current_forward_factor_percentiles(history, current)
    return current


def _percent_rank_current(
    history: list[dict[str, Any]],
    field: str,
    current_value: Any,
) -> float | None:
    current_number = _float_or_none(current_value)
    if current_number is None:
        return None
    values = [
        number
        for item in history
        if (number := _float_or_none(item.get(field))) is not None
    ]
    values.append(current_number)
    if len(values) <= 1:
        return 0.0
    less = sum(1 for value in values if value < current_number)
    return round(100.0 * less / (len(values) - 1), 2)


def _date_from_snapshot(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


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
    live = await _latest_live_payload(symbol, cache_service, repo)
    payload = {**cached, **live}
    return await _refresh_live_payload_forward_percentiles(symbol, payload, repo)


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
        result = cached
    else:
        result = await repo.history(symbol, days)
        await cache_service.set_json(cache_key, result)
    live = await _latest_live_payload(symbol, cache_service, repo)
    return _overlay_live_history(result, live)


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
            payload = (await repo.latest_live_metrics([symbol])).get(symbol)
        if not payload:
            raise HTTPException(status_code=404, detail=result)
    return await _refresh_live_payload_forward_percentiles(symbol, payload, repo)


@router.get("/live")
async def live_symbols(
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> list[dict]:
    payload = await cache_service.get_live_symbols()
    if payload:
        return [
            await _refresh_live_payload_forward_percentiles(
                str(item.get("symbol") or ""),
                item,
                repo,
            )
            for item in payload
        ]
    await fetch_and_store_live_quotes(settings, repo, cache_service.redis)
    payload = await cache_service.get_live_symbols()
    if payload:
        return [
            await _refresh_live_payload_forward_percentiles(
                str(item.get("symbol") or ""),
                item,
                repo,
            )
            for item in payload
        ]
    return [
        await _refresh_live_payload_forward_percentiles(
            str(item.get("symbol") or ""),
            item,
            repo,
        )
        for item in (await repo.latest_live_metrics()).values()
    ]


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


@router.get("/admin/kite/login-url")
async def kite_login_url(settings: Settings = Depends(get_settings)) -> dict:
    url = kite_login_url_for_settings(settings)
    if not url:
        raise HTTPException(status_code=400, detail="KITE_API_KEY is not configured")
    return {"login_url": url}


@router.post("/admin/kite/session")
async def kite_session(
    request_token: str = Body(
        default="",
        embed=True,
        description="Fresh request_token from the Kite login redirect",
    ),
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> dict:
    token = request_token.strip() or (settings.kite_request_token or "").strip()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="request_token is required in the POST body; get it from /api/admin/kite/login-url login flow",
        )
    payload = await _exchange_kite_request_token(token, settings, repo, cache_service.redis)
    return _kite_session_response(payload)


@router.get("/admin/kite/callback", response_class=HTMLResponse)
async def kite_callback(
    request_token: str = Query(default="", description="Fresh request_token from the Kite redirect"),
    status: str = Query(default="", description="Kite login status"),
    action: str = Query(default="", description="Kite login action"),
    settings: Settings = Depends(get_settings),
    repo: MarketRepository = Depends(repository),
    cache_service: CacheService = Depends(cache),
) -> HTMLResponse:
    if status and status.lower() != "success":
        return HTMLResponse(
            _kite_callback_html(
                "Kite Login Failed",
                (
                    f"Kite returned status={escape(status)!r}, "
                    f"action={escape(action)!r}. No token was stored."
                ),
            ),
            status_code=400,
        )
    token = request_token.strip()
    if not token:
        return HTMLResponse(
            _kite_callback_html(
                "Kite Login Failed",
                "The redirect did not include request_token. Please start from the Kite login URL again.",
            ),
            status_code=400,
        )
    try:
        payload = await _exchange_kite_request_token(token, settings, repo, cache_service.redis)
    except HTTPException as exc:
        return HTMLResponse(
            _kite_callback_html("Kite Login Failed", escape(str(exc.detail))),
            status_code=exc.status_code,
        )
    expires_at = escape(str(payload.get("expires_at") or "unknown"))
    login_time = escape(str(payload.get("login_time") or "unknown"))
    return HTMLResponse(
        _kite_callback_html(
            "Kite Access Token Stored",
            f"Login time: {login_time}<br>Expires at: {expires_at}",
        )
    )


async def _exchange_kite_request_token(
    request_token: str,
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
) -> dict[str, Any]:
    try:
        return await generate_kite_access_token(settings, repo, redis, request_token)
    except httpx.HTTPStatusError as exc:
        status_code = 400 if exc.response.status_code in {400, 403} else 502
        raise HTTPException(
            status_code=status_code,
            detail="Kite session exchange failed; check that request_token is fresh and from today's login redirect",
        ) from exc


def _kite_session_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "kite",
        "access_token_stored": True,
        "user_id": payload.get("user_id"),
        "api_key": payload.get("api_key"),
        "login_time": payload.get("login_time"),
        "expires_at": payload.get("expires_at"),
    }


def _kite_callback_html(title: str, body: str) -> str:
    title = escape(title)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
      body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0;
        padding: 40px;
        color: #111827;
        background: #f8fafc;
      }}
      main {{
        max-width: 620px;
        margin: 0 auto;
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 28px;
      }}
      h1 {{ margin: 0 0 12px; font-size: 24px; }}
      p {{ line-height: 1.5; }}
    </style>
  </head>
  <body>
    <main>
      <h1>{title}</h1>
      <p>{body}</p>
    </main>
  </body>
</html>"""


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
