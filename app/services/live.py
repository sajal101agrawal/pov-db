from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.core.config import Settings
from app.db.repository import MarketRepository
from app.services.cache import CacheService
from app.services.calculations import (
    constant_maturity_iv,
    forward_factor,
    forward_volatility,
    iv_slope,
)
from app.sources.dhan import (
    DhanOptionChainClient,
    normalize_market_quotes,
    normalize_option_chain,
)
from app.sources.nse_option_chain import NSEOptionChainClient
from app.sources.yahoo import YahooFinanceClient


IST = ZoneInfo("Asia/Kolkata")
_INSTRUMENT_MAP_CACHE: dict[str, dict] = {}


def parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def in_market_window(settings: Settings, now: datetime | None = None) -> bool:
    current = now.astimezone(IST) if now else datetime.now(IST)
    if current.weekday() >= 5:
        return False
    start = _parse_time(settings.live_market_start_ist)
    end = _parse_time(settings.live_market_end_ist)
    return start <= current.time() <= end


async def fetch_and_store_live_snapshots(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    if not settings.dhan_client_id or not settings.dhan_access_token:
        raise RuntimeError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for live snapshots")
    selected = symbols or parse_symbols(settings.live_symbols)
    client = DhanOptionChainClient(
        settings.dhan_client_id,
        settings.dhan_access_token,
        settings.live_option_chain_min_interval_seconds,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instrument_map = await _instrument_map(client, selected)
    cache = CacheService(redis)
    now = datetime.now(IST)
    stored = 0
    missing = []
    for symbol in selected:
        instrument = instrument_map.get(symbol)
        if not instrument:
            missing.append(symbol)
            continue
        expiries = await client.expiry_list(instrument["underlying_scrip"], instrument["underlying_seg"])
        expiry = _closest_expiry(expiries, now.date(), 30)
        if not expiry:
            missing.append(symbol)
            continue
        raw = await client.option_chain(instrument["underlying_scrip"], instrument["underlying_seg"], expiry)
        payload = normalize_option_chain(symbol, expiry, raw)
        payload.update(
            {
                "snapshot_time": now.isoformat(),
                "underlying_scrip": instrument["underlying_scrip"],
                "underlying_seg": instrument["underlying_seg"],
                "instrument_source": instrument["source"],
            }
        )
        await cache.set_live(f"chain:{symbol}", payload, ttl=max(settings.live_poll_interval_seconds * 2, 300))
        await repo.insert_live_snapshot(symbol, now, payload)
        stored += 1
    return {"symbols_requested": len(selected), "snapshots_stored": stored, "missing_symbols": missing}


async def fetch_and_store_live_quotes(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    provider = settings.live_quote_provider.lower().strip()
    if provider == "yahoo":
        return await _fetch_and_store_yahoo_live_quotes(settings, repo, redis, symbols)
    if provider == "dhan":
        return await _fetch_and_store_dhan_live_quotes(settings, repo, redis, symbols)
    raise ValueError(f"Unsupported LIVE_QUOTE_PROVIDER: {settings.live_quote_provider}")


async def _fetch_and_store_dhan_live_quotes(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    if not settings.dhan_client_id or not settings.dhan_access_token:
        raise RuntimeError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for live quotes")
    selected = symbols or await repo.active_symbols()
    selected = [symbol.upper() for symbol in selected if symbol.strip()]
    client = DhanOptionChainClient(
        settings.dhan_client_id,
        settings.dhan_access_token,
        settings.live_market_quote_min_interval_seconds,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instrument_map = await _instrument_map(client, selected)
    request: dict[str, list[int]] = {}
    reverse: dict[tuple[str, int], str] = {}
    missing = []
    for symbol in selected:
        instrument = instrument_map.get(symbol)
        if not instrument:
            missing.append(symbol)
            continue
        segment = instrument["underlying_seg"]
        security_id = instrument["underlying_scrip"]
        request.setdefault(segment, []).append(security_id)
        reverse[(segment, security_id)] = symbol
    if not request:
        return {"symbols_requested": len(selected), "quotes_stored": 0, "missing_symbols": missing}
    raw = await client.market_quote(request)
    quotes = normalize_market_quotes(reverse, raw)
    baseline = await repo.live_baseline(selected)
    option_summaries = await _fetch_live_option_summaries(settings, selected, baseline)
    cache = CacheService(redis)
    now = datetime.now(IST)
    ttl = _live_cache_ttl(settings)
    payloads = []
    for symbol, quote in quotes.items():
        payload = _live_quote_payload(
            baseline.get(symbol, {}),
            quote,
            option_summaries.get(symbol),
            now,
        )
        await cache.set_live(symbol, payload, ttl=ttl)
        payloads.append(payload)
    payloads.sort(key=lambda row: row["symbol"])
    await cache.set_live_symbols(payloads, ttl=ttl)
    return {
        "symbols_requested": len(selected),
        "quotes_stored": len(payloads),
        "missing_symbols": missing,
    }


async def _fetch_and_store_yahoo_live_quotes(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    selected = symbols or await repo.active_symbols()
    selected = [symbol.upper() for symbol in selected if symbol.strip()]
    yahoo_symbols = await repo.yahoo_symbols_for(selected)
    client = YahooFinanceClient(
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    quotes = await client.fetch_live_quotes(selected, yahoo_symbols)
    baseline = await repo.live_baseline(selected)
    option_summaries = await _fetch_live_option_summaries(settings, selected, baseline)
    cache = CacheService(redis)
    now = datetime.now(IST)
    ttl = _live_cache_ttl(settings)
    payloads = []
    missing = []
    for symbol in selected:
        quote = quotes.get(symbol)
        if not quote:
            missing.append(symbol)
            continue
        base = baseline.get(symbol, {})
        payload = _live_quote_payload(base, quote, option_summaries.get(symbol), now)
        await cache.set_live(symbol, payload, ttl=ttl)
        payloads.append(payload)
    payloads.sort(key=lambda row: row["symbol"])
    await cache.set_live_symbols(payloads, ttl=ttl)
    return {
        "symbols_requested": len(selected),
        "quotes_stored": len(payloads),
        "missing_symbols": missing,
        "provider": "yahoo",
        "ttl_seconds": ttl,
    }


async def _fetch_live_option_summaries(
    settings: Settings,
    symbols: list[str],
    baseline: dict[str, dict],
) -> dict[str, dict]:
    provider = settings.live_option_summary_provider.lower().strip()
    if provider in {"", "none", "disabled"}:
        return {}
    if provider != "nse":
        raise ValueError(
            f"Unsupported LIVE_OPTION_SUMMARY_PROVIDER: {settings.live_option_summary_provider}"
        )
    expiry_hints = {
        symbol: {
            "expiry_30d": baseline.get(symbol, {}).get("expiry_30d"),
            "expiry_60d": baseline.get(symbol, {}).get("expiry_60d"),
            "expiry_90d": baseline.get(symbol, {}).get("expiry_90d"),
        }
        for symbol in symbols
    }
    client = NSEOptionChainClient(
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
        settings.live_option_summary_concurrency,
        settings.live_option_summary_min_interval_seconds,
    )
    try:
        return await client.fetch_summaries(symbols, expiry_hints)
    except Exception:
        return {}


async def live_worker_loop(settings: Settings, repo: MarketRepository, redis: Redis) -> None:
    while True:
        try:
            if in_market_window(settings):
                await fetch_and_store_live_quotes(settings, repo, redis)
        except Exception as exc:  # noqa: BLE001 - worker must keep running
            await repo.log_error(
                "live_snapshot_worker",
                type(exc).__name__,
                {"message": str(exc), "repr": repr(exc)},
                source=settings.live_quote_provider,
            )
        await asyncio.sleep(settings.live_poll_interval_seconds)


def _closest_expiry(expiries, trade_date, target_dte: int):
    future = [expiry for expiry in expiries if expiry >= trade_date]
    if not future:
        return None
    return min(future, key=lambda expiry: abs((expiry - trade_date).days - target_dte))


async def _instrument_map(client: DhanOptionChainClient, symbols: list[str]) -> dict[str, dict]:
    missing = [symbol for symbol in symbols if symbol not in _INSTRUMENT_MAP_CACHE]
    if missing:
        _INSTRUMENT_MAP_CACHE.update(await client.instrument_map(set(missing)))
    return {symbol: _INSTRUMENT_MAP_CACHE[symbol] for symbol in symbols if symbol in _INSTRUMENT_MAP_CACHE}


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute), tzinfo=IST)


def _live_cache_ttl(settings: Settings) -> int:
    return max(1, int(settings.live_cache_ttl_seconds))


def _live_quote_payload(
    base: dict[str, Any],
    quote: dict[str, Any],
    option_summary: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    payload = {
        **base,
        **quote,
        "snapshot_time": now.isoformat(),
        "quote_type": "basic",
    }

    if option_summary:
        _preserve_eod_values(
            payload,
            base,
            [
                "avg_option_volume",
                "iv_30",
                "iv_60",
                "iv_90",
                "fwdv_3060",
                "fwdfct_3060",
                "fev_30",
                "iv_slope_3060",
            ],
        )
        payload.update(option_summary)
        payload["avg_option_volume"] = option_summary["live_option_volume"]
        payload["avg_option_volume_source"] = option_summary["live_option_volume_source"]
        payload["avg_option_volume_kind"] = option_summary["live_option_volume_kind"]
        payload.update(_live_forward_metrics(option_summary, now.date()))
    elif base.get("avg_option_volume") is not None:
        payload["avg_option_volume_source"] = "symbol_daily_metrics"

    if base.get("avg_option_volume") is not None:
        payload.setdefault("avg_option_volume_kind", "eod_total_contracts_all_strikes")
    for key in ("iv_30", "iv_60", "iv_90"):
        if base.get(key) is not None:
            payload.setdefault(f"{key}_source", "symbol_daily_metrics")
    if base.get("fwdfct_3060") is not None:
        payload.setdefault("forward_analytics_source", "symbol_daily_metrics")
    if base.get("iv_slope_3060") is not None:
        payload.setdefault("iv_slope_3060_source", "symbol_daily_metrics")
    return payload


def _preserve_eod_values(payload: dict[str, Any], base: dict[str, Any], keys: list[str]) -> None:
    for key in keys:
        if base.get(key) is not None:
            payload[f"eod_{key}"] = base.get(key)


def _live_forward_metrics(option_summary: dict[str, Any], trade_date: date) -> dict[str, Any]:
    terms = _live_iv_terms(option_summary, trade_date)
    if not terms:
        return {}

    iv30 = _constant_maturity_from_terms(terms, 30)
    iv60 = _constant_maturity_from_terms(terms, 60)
    iv90 = _constant_maturity_from_terms(terms, 90)
    fwdv = forward_volatility(iv30, iv60, 30, 60)
    metrics: dict[str, Any] = {
        "iv_30": iv30,
        "iv_60": iv60,
        "iv_90": iv90,
        "fwdv_3060": fwdv,
        "fwdfct_3060": forward_factor(iv30, fwdv),
        "fev_30": fwdv,
        "iv_slope_3060": iv_slope(iv30, iv60, 30, 60),
        "iv_term_structure_source": "nse:option-chain-v3",
        "forward_analytics_source": "nse:option-chain-v3",
        "iv_slope_3060_source": "nse:option-chain-v3",
        "live_iv_term_structure": [
            {"tenor": 30, "iv": iv30},
            {"tenor": 60, "iv": iv60},
            {"tenor": 90, "iv": iv90},
        ],
    }
    for key in ("iv_30", "iv_60", "iv_90"):
        if metrics.get(key) is not None:
            metrics[f"{key}_source"] = "nse:option-chain-v3"

    for index, tenor in enumerate((30, 60, 90)):
        if index < len(terms):
            metrics[f"expiry_{tenor}d"] = terms[index]["expiry_date"]
            metrics[f"dte_{tenor}"] = terms[index]["dte"]
    return metrics


def _live_iv_terms(option_summary: dict[str, Any], trade_date: date) -> list[dict[str, Any]]:
    raw_terms = option_summary.get("live_iv_terms") or [
        {
            "expiry": option_summary.get("live_option_expiry"),
            "expiry_date": option_summary.get("live_option_expiry_date"),
            "atm_iv": option_summary.get("live_atm_iv"),
        }
    ]
    terms = []
    for item in raw_terms:
        expiry_date = _coerce_date(item.get("expiry_date") or item.get("expiry"))
        atm_iv_value = _coerce_float(item.get("atm_iv"))
        if expiry_date is None or atm_iv_value is None or atm_iv_value <= 0:
            continue
        dte = (expiry_date - trade_date).days
        if dte <= 0:
            continue
        terms.append({"expiry_date": expiry_date, "dte": dte, "iv": atm_iv_value})
    return sorted(terms, key=lambda item: (item["dte"], item["expiry_date"]))


def _constant_maturity_from_terms(terms: list[dict[str, Any]], target_dte: int) -> float | None:
    exact = next((item["iv"] for item in terms if item["dte"] == target_dte), None)
    if exact is not None:
        return exact
    below = [item for item in terms if item["dte"] < target_dte]
    above = [item for item in terms if item["dte"] > target_dte]
    if below and above:
        near = max(below, key=lambda item: item["dte"])
        far = min(above, key=lambda item: item["dte"])
        return constant_maturity_iv(near["iv"], near["dte"], far["iv"], far["dte"], target_dte)
    return min(terms, key=lambda item: abs(item["dte"] - target_dte))["iv"]


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
