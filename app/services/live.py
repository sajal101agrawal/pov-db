from __future__ import annotations

import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.core.config import Settings
from app.db.repository import MarketRepository
from app.services.cache import CacheService
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
    cache = CacheService(redis)
    now = datetime.now(IST)
    ttl = _live_cache_ttl(settings)
    payloads = []
    for symbol, quote in quotes.items():
        payload = {
            **baseline.get(symbol, {}),
            **quote,
            "snapshot_time": now.isoformat(),
            "quote_type": "basic",
        }
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
        payload = {
            **base,
            **quote,
            "snapshot_time": now.isoformat(),
            "quote_type": "basic",
        }
        option_summary = option_summaries.get(symbol)
        if option_summary:
            if base.get("avg_option_volume") is not None:
                payload["eod_avg_option_volume"] = base.get("avg_option_volume")
            payload.update(option_summary)
            payload["avg_option_volume"] = option_summary["live_option_volume"]
            payload["avg_option_volume_source"] = option_summary["live_option_volume_source"]
            payload["avg_option_volume_kind"] = option_summary["live_option_volume_kind"]
        elif base.get("avg_option_volume") is not None:
            payload["avg_option_volume_source"] = "symbol_daily_metrics"
        if base.get("avg_option_volume") is not None:
            payload.setdefault("avg_option_volume_kind", "eod_total_contracts_all_strikes")
        if base.get("iv_30") is not None:
            payload["iv_30_source"] = "symbol_daily_metrics"
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
    expiry_hints = {symbol: baseline.get(symbol, {}).get("expiry_30d") for symbol in symbols}
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
