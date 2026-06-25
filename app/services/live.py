from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta
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
    implied_volatility_bisection,
    iv_slope,
    years_to_expiry,
)
from app.sources.dhan import (
    DhanOptionChainClient,
    combine_expiry_summaries,
    normalize_option_chain_summary as normalize_dhan_option_chain_summary,
    normalize_market_quotes,
    normalize_option_chain,
    token_expiry,
)
from app.sources.kite import (
    KiteConnectClient,
    login_url as kite_login_url,
    normalize_market_quotes as normalize_kite_market_quotes,
    quote_last_price as kite_quote_last_price,
    quote_mid_or_ltp as kite_quote_mid_or_ltp,
    token_login_time as kite_token_login_time,
)
from app.sources.nse_option_chain import NSEOptionChainClient
from app.sources.yahoo import YahooFinanceClient


IST = ZoneInfo("Asia/Kolkata")
_INSTRUMENT_MAP_CACHE: dict[str, dict] = {}
_KITE_INSTRUMENT_CACHE: list[dict[str, Any]] | None = None
_KITE_TOKEN_REFRESH_LAST_DATE: date | None = None
_ONE_DAY = timedelta(days=1)


def parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


async def selected_live_symbols(
    settings: Settings,
    repo: MarketRepository,
    symbols: list[str] | None = None,
) -> list[str]:
    if symbols is not None:
        return [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    configured = settings.live_symbols.strip()
    if configured.lower() in {"", "all", "*"}:
        return await repo.active_symbols()
    return parse_symbols(configured)


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
    provider = settings.live_option_chain_provider.lower().strip()
    if provider == "nse":
        return await _fetch_and_store_nse_live_snapshots(settings, repo, redis, symbols)
    if provider == "kite":
        try:
            return await _fetch_and_store_kite_live_snapshots(settings, repo, redis, symbols)
        except Exception as exc:
            await repo.log_error(
                "live_snapshot_provider_fallback",
                type(exc).__name__,
                {
                    "message": str(exc),
                    "repr": repr(exc),
                    "provider": "kite",
                    "fallback_provider": "nse",
                    "symbols": symbols,
                },
                source="kite:fallback_to_nse",
            )
            return await _fetch_and_store_nse_live_snapshots(settings, repo, redis, symbols)
    if provider != "dhan":
        raise ValueError(f"Unsupported LIVE_OPTION_CHAIN_PROVIDER: {settings.live_option_chain_provider}")

    try:
        return await _fetch_and_store_dhan_live_snapshots(settings, repo, redis, symbols)
    except Exception as exc:
        await repo.log_error(
            "live_snapshot_provider_fallback",
            type(exc).__name__,
            {
                "message": str(exc),
                "repr": repr(exc),
                "provider": "dhan",
                "fallback_provider": "nse",
                "symbols": symbols,
            },
            source="dhan:fallback_to_nse",
        )
        return await _fetch_and_store_nse_live_snapshots(settings, repo, redis, symbols)


async def _fetch_and_store_dhan_live_snapshots(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    if not settings.dhan_client_id:
        raise RuntimeError("DHAN_CLIENT_ID is required for live snapshots")
    access_token = await _dhan_access_token(settings, redis)
    selected = await selected_live_symbols(settings, repo, symbols)
    client = DhanOptionChainClient(
        settings.dhan_client_id,
        access_token,
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


async def _fetch_and_store_nse_live_snapshots(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    selected = await selected_live_symbols(settings, repo, symbols)
    baseline = await repo.live_baseline([symbol.upper() for symbol in selected])
    client = NSEOptionChainClient(
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
        settings.live_option_summary_concurrency,
        settings.live_option_summary_min_interval_seconds,
    )
    cache = CacheService(redis)
    now = datetime.now(IST)
    stored = 0
    missing = []
    ttl = max(settings.live_poll_interval_seconds * 2, 300)
    for symbol in selected:
        symbol = symbol.upper()
        payload = await client.fetch_chain(symbol, baseline.get(symbol, {}).get("expiry_30d"))
        if not payload:
            missing.append(symbol)
            continue
        payload.update(
            {
                "snapshot_time": now.isoformat(),
                "instrument_source": "nse:option-chain-v3",
            }
        )
        await cache.set_live(f"chain:{symbol}", payload, ttl=ttl)
        await repo.insert_live_snapshot(symbol, now, payload)
        stored += 1
    return {"symbols_requested": len(selected), "snapshots_stored": stored, "missing_symbols": missing}


async def _fetch_and_store_kite_live_snapshots(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    if not settings.kite_api_key:
        raise RuntimeError("KITE_API_KEY is required for Kite live snapshots")
    access_token = await _kite_access_token(settings, repo, redis)
    selected = await selected_live_symbols(settings, repo, symbols)
    client = KiteConnectClient(
        settings.kite_api_key,
        access_token,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instruments = await _kite_instruments(client)
    now = datetime.now(IST)
    trade_date = now.date()
    rate = await repo.risk_free_rate(trade_date, settings.default_risk_free_rate)
    cache = CacheService(redis)
    ttl = max(settings.live_poll_interval_seconds * 2, 300)
    stored = 0
    missing = []
    for symbol in selected:
        symbol = symbol.upper()
        spot_payload = await _kite_quote_many(settings, client, [f"NSE:{symbol}"])
        spot = kite_quote_last_price(spot_payload, f"NSE:{symbol}")
        if spot is None:
            missing.append(symbol)
            continue
        rows = _kite_option_rows(instruments, symbol)
        expiry = _closest_expiry(_kite_expiry_targets(rows, trade_date), trade_date, 30)
        if expiry is None:
            missing.append(symbol)
            continue
        expiry_rows = [row for row in rows if _coerce_date(row.get("expiry")) == expiry]
        quote_keys = [_kite_instrument_key(row) for row in expiry_rows]
        quote_payload = await _kite_quote_many(settings, client, [key for key in quote_keys if key])
        payload = _kite_option_chain_payload(symbol, spot, expiry, expiry_rows, quote_payload, trade_date, rate)
        if not payload:
            missing.append(symbol)
            continue
        payload.update(
            {
                "snapshot_time": now.isoformat(),
                "instrument_source": "kite:instruments",
            }
        )
        await cache.set_live(f"chain:{symbol}", payload, ttl=ttl)
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
        try:
            return await _fetch_and_store_dhan_live_quotes(settings, repo, redis, symbols)
        except Exception as exc:
            await repo.log_error(
                "live_quote_provider_fallback",
                type(exc).__name__,
                {
                    "message": str(exc),
                    "repr": repr(exc),
                    "provider": "dhan",
                    "fallback_provider": "yahoo",
                    "symbols": symbols,
                },
                source="dhan:fallback_to_yahoo",
            )
            return await _fetch_and_store_yahoo_live_quotes(settings, repo, redis, symbols)
    if provider == "kite":
        try:
            return await _fetch_and_store_kite_live_quotes(settings, repo, redis, symbols)
        except Exception as exc:
            await repo.log_error(
                "live_quote_provider_fallback",
                type(exc).__name__,
                {
                    "message": str(exc),
                    "repr": repr(exc),
                    "provider": "kite",
                    "fallback_provider": "yahoo",
                    "symbols": symbols,
                },
                source="kite:fallback_to_yahoo",
            )
            return await _fetch_and_store_yahoo_live_quotes(settings, repo, redis, symbols)
    raise ValueError(f"Unsupported LIVE_QUOTE_PROVIDER: {settings.live_quote_provider}")


async def _fetch_and_store_dhan_live_quotes(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    if not settings.dhan_client_id:
        raise RuntimeError("DHAN_CLIENT_ID is required for live quotes")
    access_token = await _dhan_access_token(settings, redis)
    selected = await selected_live_symbols(settings, repo, symbols)
    client = DhanOptionChainClient(
        settings.dhan_client_id,
        access_token,
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
    option_summaries = await _fetch_live_option_summaries(settings, repo, redis, selected, baseline)
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
    await repo.upsert_live_symbol_metrics(payloads)
    return {
        "symbols_requested": len(selected),
        "quotes_stored": len(payloads),
        "missing_symbols": missing,
    }


async def _fetch_and_store_kite_live_quotes(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    if not settings.kite_api_key:
        raise RuntimeError("KITE_API_KEY is required for Kite live quotes")
    access_token = await _kite_access_token(settings, repo, redis)
    selected = await selected_live_symbols(settings, repo, symbols)
    symbol_to_key = {symbol: f"NSE:{symbol}" for symbol in selected}
    client = KiteConnectClient(
        settings.kite_api_key,
        access_token,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    raw = await _kite_quote_many(settings, client, list(symbol_to_key.values()))
    quotes = normalize_kite_market_quotes(raw, symbol_to_key)
    baseline = await repo.live_baseline(selected)
    option_summaries = await _fetch_live_option_summaries(settings, repo, redis, selected, baseline)
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
    await repo.upsert_live_symbol_metrics(payloads)
    return {
        "symbols_requested": len(selected),
        "quotes_stored": len(payloads),
        "missing_symbols": missing,
        "provider": "kite",
        "ttl_seconds": ttl,
    }


async def _fetch_and_store_yahoo_live_quotes(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str] | None = None,
) -> dict:
    selected = await selected_live_symbols(settings, repo, symbols)
    yahoo_symbols = await repo.yahoo_symbols_for(selected)
    client = YahooFinanceClient(
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    quotes = await client.fetch_live_quotes(selected, yahoo_symbols)
    baseline = await repo.live_baseline(selected)
    option_summaries = await _fetch_live_option_summaries(settings, repo, redis, selected, baseline)
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
    await repo.upsert_live_symbol_metrics(payloads)
    return {
        "symbols_requested": len(selected),
        "quotes_stored": len(payloads),
        "missing_symbols": missing,
        "provider": "yahoo",
        "ttl_seconds": ttl,
    }


async def _fetch_live_option_summaries(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str],
    baseline: dict[str, dict],
) -> dict[str, dict]:
    provider = settings.live_option_summary_provider.lower().strip()
    if provider in {"", "none", "disabled"}:
        return {}
    if provider == "dhan":
        try:
            dhan_summaries = await _fetch_dhan_live_option_summaries(
                settings, redis, symbols, baseline
            )
        except Exception as exc:
            await repo.log_error(
                "live_option_summary_provider_fallback",
                type(exc).__name__,
                {
                    "message": str(exc),
                    "repr": repr(exc),
                    "provider": "dhan",
                    "fallback_provider": "nse",
                    "symbols": symbols,
                },
                source="dhan:fallback_to_nse",
            )
            return await _fetch_nse_live_option_summaries(settings, symbols, baseline)

        missing = [symbol for symbol in symbols if symbol not in dhan_summaries]
        if missing:
            nse_summaries = await _fetch_nse_live_option_summaries(settings, missing, baseline)
            return {**nse_summaries, **dhan_summaries}
        return dhan_summaries
    if provider == "kite":
        try:
            kite_summaries = await _fetch_kite_live_option_summaries(
                settings, repo, redis, symbols, baseline
            )
        except Exception as exc:
            await repo.log_error(
                "live_option_summary_provider_fallback",
                type(exc).__name__,
                {
                    "message": str(exc),
                    "repr": repr(exc),
                    "provider": "kite",
                    "fallback_provider": "nse",
                    "symbols": symbols,
                },
                source="kite:fallback_to_nse",
            )
            return await _fetch_nse_live_option_summaries(settings, symbols, baseline)

        missing = [symbol for symbol in symbols if symbol not in kite_summaries]
        if missing:
            nse_summaries = await _fetch_nse_live_option_summaries(settings, missing, baseline)
            return {**nse_summaries, **kite_summaries}
        return kite_summaries
    if provider != "nse":
        raise ValueError(
            f"Unsupported LIVE_OPTION_SUMMARY_PROVIDER: {settings.live_option_summary_provider}"
        )
    return await _fetch_nse_live_option_summaries(settings, symbols, baseline)


async def _fetch_nse_live_option_summaries(
    settings: Settings,
    symbols: list[str],
    baseline: dict[str, dict],
) -> dict[str, dict]:
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


async def _fetch_dhan_live_option_summaries(
    settings: Settings,
    redis: Redis,
    symbols: list[str],
    baseline: dict[str, dict],
) -> dict[str, dict]:
    if not settings.dhan_client_id:
        raise RuntimeError("DHAN_CLIENT_ID is required for Dhan option summaries")
    access_token = await _dhan_access_token(settings, redis)
    selected = [symbol.upper() for symbol in symbols]
    client = DhanOptionChainClient(
        settings.dhan_client_id,
        access_token,
        0.0,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instrument_map = await _instrument_map(client, selected)
    now = datetime.now(IST).date()
    requests: list[tuple[str, dict[str, Any], date]] = []
    for symbol in selected:
        instrument = instrument_map.get(symbol)
        if not instrument:
            continue
        targets = _dhan_expiry_targets_from_baseline(baseline.get(symbol, {}))
        if not targets:
            expiries = await client.expiry_list(
                instrument["underlying_scrip"],
                instrument["underlying_seg"],
            )
            targets = [
                expiry
                for expiry in (
                    _closest_expiry(expiries, now, 30),
                    _closest_expiry(expiries, now, 60),
                    _closest_expiry(expiries, now, 90),
                )
                if expiry is not None
            ]
        seen: set[date] = set()
        for expiry in targets:
            if expiry in seen:
                continue
            seen.add(expiry)
            requests.append((symbol, instrument, expiry))

    summaries_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in selected}
    batch_size = max(1, int(settings.live_dhan_option_summary_batch_size))
    batch_delay = max(0.0, float(settings.live_dhan_option_summary_batch_delay_seconds))
    for index in range(0, len(requests), batch_size):
        batch = requests[index : index + batch_size]
        results = await asyncio.gather(
            *[_fetch_one_dhan_option_summary(client, item) for item in batch],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception) or result is None:
                continue
            symbol, summary = result
            summaries_by_symbol.setdefault(symbol, []).append(summary)
        if index + batch_size < len(requests) and batch_delay:
            await asyncio.sleep(batch_delay)

    output = {}
    for symbol, summaries in summaries_by_symbol.items():
        summaries.sort(key=lambda item: item["live_option_expiry_date"])
        combined = combine_expiry_summaries(symbol, summaries)
        if combined:
            output[symbol] = combined
    return output


async def _fetch_one_dhan_option_summary(
    client: DhanOptionChainClient,
    item: tuple[str, dict[str, Any], date],
) -> tuple[str, dict[str, Any]] | None:
    symbol, instrument, expiry = item
    raw = await client.option_chain(
        instrument["underlying_scrip"],
        instrument["underlying_seg"],
        expiry,
    )
    summary = normalize_dhan_option_chain_summary(symbol, expiry, raw)
    return (symbol, summary) if summary else None


async def _fetch_kite_live_option_summaries(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    symbols: list[str],
    baseline: dict[str, dict],
) -> dict[str, dict]:
    if not settings.kite_api_key:
        raise RuntimeError("KITE_API_KEY is required for Kite option summaries")
    access_token = await _kite_access_token(settings, repo, redis)
    selected = [symbol.upper() for symbol in symbols]
    client = KiteConnectClient(
        settings.kite_api_key,
        access_token,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    instruments = await _kite_instruments(client)
    now = datetime.now(IST).date()
    rate = await repo.risk_free_rate(now, settings.default_risk_free_rate)
    underlying_keys = {symbol: f"NSE:{symbol}" for symbol in selected}
    underlying_quotes = await _kite_quote_many(settings, client, list(underlying_keys.values()))

    requests: list[dict[str, Any]] = []
    for symbol in selected:
        spot = kite_quote_last_price(underlying_quotes, underlying_keys[symbol])
        if spot is None:
            continue
        rows = _kite_option_rows(instruments, symbol)
        targets = _dhan_expiry_targets_from_baseline(baseline.get(symbol, {}))
        if not targets:
            targets = _kite_expiry_targets(rows, now)
        for expiry in targets:
            request = _kite_atm_option_request(symbol, spot, rows, expiry)
            if request:
                requests.append(request)

    quote_keys = sorted(
        {
            key
            for request in requests
            for key in (request.get("ce_key"), request.get("pe_key"))
            if key
        }
    )
    option_quotes = await _kite_quote_many(settings, client, quote_keys)
    summaries_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in selected}
    for request in requests:
        summary = _kite_option_summary_from_quotes(request, option_quotes, now, rate)
        if summary:
            summaries_by_symbol.setdefault(request["symbol"], []).append(summary)

    output = {}
    for symbol, summaries in summaries_by_symbol.items():
        summaries.sort(key=lambda item: item["live_option_expiry_date"])
        combined = combine_expiry_summaries(symbol, summaries)
        if combined:
            output[symbol] = combined
    return output


async def live_worker_loop(settings: Settings, repo: MarketRepository, redis: Redis) -> None:
    while True:
        try:
            await _maybe_refresh_kite_access_token(settings, repo, redis)
            if await _should_poll_live(settings, repo):
                await fetch_and_store_live_quotes(settings, repo, redis)
        except Exception as exc:  # noqa: BLE001 - worker must keep running
            await repo.log_error(
                "live_snapshot_worker",
                type(exc).__name__,
                {"message": str(exc), "repr": repr(exc)},
                source=settings.live_quote_provider,
            )
        await asyncio.sleep(settings.live_poll_interval_seconds)


async def _should_poll_live(settings: Settings, repo: MarketRepository) -> bool:
    if not in_market_window(settings):
        return False
    today = datetime.now(IST).date()
    is_trading_day = await repo.pool.fetchval(
        "SELECT is_trading_day FROM trading_calendar WHERE trade_date = $1",
        today,
    )
    return is_trading_day is not False


async def _dhan_access_token(settings: Settings, redis: Redis | None = None) -> str:
    if not settings.dhan_client_id:
        raise RuntimeError("DHAN_CLIENT_ID is required for Dhan live data")

    can_generate = bool(settings.dhan_pin and settings.dhan_totp_secret)
    cache_key = f"dhan:access-token:{settings.dhan_client_id}"
    if can_generate and redis is not None:
        cached = await redis.get(cache_key)
        if cached:
            cached_payload = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
            token = cached_payload.get("accessToken")
            expiry = _dhan_token_expiry(cached_payload)
            if token and _token_is_usable(expiry, settings.dhan_token_refresh_margin_seconds):
                return str(token)

    if can_generate:
        try:
            payload = await DhanOptionChainClient.generate_access_token(
                settings.dhan_client_id,
                str(settings.dhan_pin),
                str(settings.dhan_totp_secret),
                settings.source_retry_attempts,
                settings.source_retry_base_delay_seconds,
                settings.source_retry_max_delay_seconds,
            )
            expiry = _dhan_token_expiry(payload)
            if redis is not None:
                ttl = _token_cache_ttl_seconds(expiry, settings.dhan_token_refresh_margin_seconds)
                await redis.set(cache_key, json.dumps(payload, default=str), ex=ttl)
            return str(payload["accessToken"])
        except Exception:
            if settings.dhan_access_token:
                return settings.dhan_access_token
            raise

    if settings.dhan_access_token:
        return settings.dhan_access_token
    raise RuntimeError(
        "DHAN_ACCESS_TOKEN or DHAN_PIN + DHAN_TOTP_SECRET is required for Dhan live data"
    )


def _dhan_token_expiry(payload: dict[str, Any]) -> datetime | None:
    expiry = token_expiry(payload)
    if expiry is None:
        return None
    return expiry if expiry.tzinfo else expiry.replace(tzinfo=IST)


def _token_is_usable(expiry: datetime | None, refresh_margin_seconds: int) -> bool:
    if expiry is None:
        return False
    now = datetime.now(expiry.tzinfo)
    return (expiry - now).total_seconds() > max(0, refresh_margin_seconds)


def _token_cache_ttl_seconds(expiry: datetime | None, refresh_margin_seconds: int) -> int:
    if expiry is None:
        return 23 * 60 * 60
    now = datetime.now(expiry.tzinfo)
    ttl = int((expiry - now).total_seconds()) - max(0, refresh_margin_seconds)
    return max(60, ttl)


async def generate_kite_access_token(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis | None,
    request_token: str,
) -> dict[str, Any]:
    if not settings.kite_api_key or not settings.kite_api_secret:
        raise RuntimeError("KITE_API_KEY and KITE_API_SECRET are required for Kite token exchange")
    payload = await KiteConnectClient.generate_session(
        settings.kite_api_key,
        settings.kite_api_secret,
        request_token,
        settings.source_retry_attempts,
        settings.source_retry_base_delay_seconds,
        settings.source_retry_max_delay_seconds,
    )
    expiry = _kite_access_token_expiry(kite_token_login_time(payload))
    stored_payload = {
        **payload,
        "expires_at": expiry.isoformat(),
    }
    access_token = str(payload["access_token"])
    await repo.upsert_broker_access_token("kite", access_token, expiry, stored_payload)
    if redis is not None:
        await _cache_kite_token(settings, redis, stored_payload, expiry)
    return stored_payload


async def _kite_access_token(settings: Settings, repo: MarketRepository, redis: Redis | None = None) -> str:
    if not settings.kite_api_key:
        raise RuntimeError("KITE_API_KEY is required for Kite live data")
    cache_key = f"kite:access-token:{settings.kite_api_key}"
    if redis is not None:
        cached = await redis.get(cache_key)
        if cached:
            cached_payload = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
            token = cached_payload.get("access_token")
            expiry = _kite_expiry_from_payload(cached_payload)
            if token and _token_is_usable(expiry, 300):
                return str(token)

    stored = await repo.broker_access_token("kite")
    if stored:
        token = stored.get("access_token")
        expiry = _kite_expiry_from_payload(stored)
        if token and _token_is_usable(expiry, 300):
            if redis is not None:
                await _cache_kite_token(settings, redis, stored, expiry)
            return str(token)

    if settings.kite_request_token:
        return str(
            (
                await generate_kite_access_token(
                    settings, repo, redis, settings.kite_request_token
                )
            )["access_token"]
        )

    if settings.kite_access_token:
        return settings.kite_access_token
    raise RuntimeError(
        "KITE_ACCESS_TOKEN or a fresh KITE_REQUEST_TOKEN is required for Kite live data"
    )


async def _cache_kite_token(
    settings: Settings,
    redis: Redis,
    payload: dict[str, Any],
    expiry: datetime | None,
) -> None:
    if not settings.kite_api_key:
        return
    ttl = _token_cache_ttl_seconds(expiry, 300)
    await redis.set(
        f"kite:access-token:{settings.kite_api_key}",
        json.dumps(payload, default=str),
        ex=ttl,
    )


async def _maybe_refresh_kite_access_token(
    settings: Settings,
    repo: MarketRepository,
    redis: Redis,
    now: datetime | None = None,
) -> None:
    global _KITE_TOKEN_REFRESH_LAST_DATE
    if not settings.kite_auto_refresh_enabled or not _settings_use_kite(settings):
        return
    current = now.astimezone(IST) if now else datetime.now(IST)
    refresh_time = _parse_time(settings.kite_token_refresh_time_ist)
    if current.time() < refresh_time or _KITE_TOKEN_REFRESH_LAST_DATE == current.date():
        return
    _KITE_TOKEN_REFRESH_LAST_DATE = current.date()
    if not settings.kite_request_token:
        await repo.log_error(
            "kite_token_refresh",
            "MissingRequestToken",
            {
                "message": (
                    "Kite cannot generate an access token without a fresh request_token "
                    "from the daily login flow."
                ),
                "login_url": kite_login_url_for_settings(settings),
                "refresh_time_ist": settings.kite_token_refresh_time_ist,
            },
            source="kite:auth",
        )
        return
    try:
        await generate_kite_access_token(settings, repo, redis, settings.kite_request_token)
    except Exception as exc:
        await repo.log_error(
            "kite_token_refresh",
            type(exc).__name__,
            {
                "message": str(exc),
                "repr": repr(exc),
                "login_url": kite_login_url_for_settings(settings),
            },
            source="kite:auth",
        )


def kite_login_url_for_settings(settings: Settings) -> str | None:
    return kite_login_url(settings.kite_api_key) if settings.kite_api_key else None


def _settings_use_kite(settings: Settings) -> bool:
    return "kite" in {
        settings.live_quote_provider.lower().strip(),
        settings.live_option_summary_provider.lower().strip(),
        settings.live_option_chain_provider.lower().strip(),
    }


def _kite_access_token_expiry(login_time: datetime | None = None) -> datetime:
    base = login_time or datetime.now(IST)
    if base.tzinfo is None:
        base = base.replace(tzinfo=IST)
    expiry = datetime.combine(base.date(), time(6, 0), tzinfo=IST)
    if expiry <= base:
        expiry = expiry + _ONE_DAY
    return expiry


def _kite_expiry_from_payload(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("expires_at")
    if not value:
        return None
    parsed = _coerce_datetime_value(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


async def _kite_instruments(client: KiteConnectClient) -> list[dict[str, Any]]:
    global _KITE_INSTRUMENT_CACHE
    if _KITE_INSTRUMENT_CACHE is None:
        _KITE_INSTRUMENT_CACHE = await client.instruments("NFO")
    return _KITE_INSTRUMENT_CACHE


async def _kite_quote_many(
    settings: Settings,
    client: KiteConnectClient,
    instrument_keys: list[str],
) -> dict[str, Any]:
    clean = list(dict.fromkeys(key for key in instrument_keys if key))
    data: dict[str, Any] = {}
    batch_size = max(1, min(int(settings.live_kite_quote_batch_size), 500))
    delay = max(0.0, float(settings.live_kite_quote_batch_delay_seconds))
    for index in range(0, len(clean), batch_size):
        batch = clean[index : index + batch_size]
        payload = await client.quote(batch)
        data.update(payload.get("data") or {})
        if index + batch_size < len(clean) and delay:
            await asyncio.sleep(delay)
    return {"status": "success", "data": data}


def _kite_option_rows(instruments: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    symbol = symbol.upper()
    return [
        row
        for row in instruments
        if (row.get("exchange") or "").upper() == "NFO"
        and (row.get("segment") or "").upper().startswith("NFO-OPT")
        and (row.get("name") or "").upper() == symbol
        and (row.get("instrument_type") or "").upper() in {"CE", "PE"}
        and _coerce_date(row.get("expiry")) is not None
        and _coerce_float(row.get("strike")) is not None
    ]


def _kite_expiry_targets(rows: list[dict[str, Any]], trade_date: date) -> list[date]:
    expiries = sorted(
        {
            expiry
            for row in rows
            if (expiry := _coerce_date(row.get("expiry"))) is not None
            and expiry >= trade_date
        }
    )
    targets = []
    seen = set()
    for target_dte in (30, 60, 90):
        expiry = _closest_expiry(expiries, trade_date, target_dte)
        if expiry is not None and expiry not in seen:
            targets.append(expiry)
            seen.add(expiry)
    return targets


def _kite_atm_option_request(
    symbol: str,
    spot: float,
    rows: list[dict[str, Any]],
    expiry: date,
) -> dict[str, Any] | None:
    expiry_rows = [
        row
        for row in rows
        if _coerce_date(row.get("expiry")) == expiry
    ]
    strikes = sorted(
        {
            strike
            for row in expiry_rows
            if (strike := _coerce_float(row.get("strike"))) is not None
        }
    )
    if not strikes:
        return None
    atm_strike = min(strikes, key=lambda strike: (abs(strike - spot), strike))
    ce = _kite_option_row(expiry_rows, atm_strike, "CE")
    pe = _kite_option_row(expiry_rows, atm_strike, "PE")
    if ce is None and pe is None:
        return None
    return {
        "symbol": symbol.upper(),
        "spot": spot,
        "expiry": expiry,
        "strike": atm_strike,
        "strike_count": len(strikes),
        "ce_row": ce,
        "pe_row": pe,
        "ce_key": _kite_instrument_key(ce) if ce else None,
        "pe_key": _kite_instrument_key(pe) if pe else None,
    }


def _kite_option_row(rows: list[dict[str, Any]], strike: float, option_type: str) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in rows
            if _coerce_float(row.get("strike")) == strike
            and (row.get("instrument_type") or "").upper() == option_type
        ),
        None,
    )


def _kite_instrument_key(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    tradingsymbol = row.get("tradingsymbol")
    return f"NFO:{tradingsymbol}" if tradingsymbol else None


def _kite_option_summary_from_quotes(
    request: dict[str, Any],
    option_quotes: dict[str, Any],
    trade_date: date,
    risk_free_rate: float,
) -> dict[str, Any] | None:
    data = option_quotes.get("data") or {}
    ce_quote = data.get(request.get("ce_key")) if request.get("ce_key") else None
    pe_quote = data.get(request.get("pe_key")) if request.get("pe_key") else None
    if not ce_quote and not pe_quote:
        return None
    spot = request["spot"]
    strike = request["strike"]
    expiry = request["expiry"]
    call_iv = _kite_leg_iv(ce_quote, spot, strike, expiry, trade_date, risk_free_rate, "CE")
    put_iv = _kite_leg_iv(pe_quote, spot, strike, expiry, trade_date, risk_free_rate, "PE")
    atm_iv = _average_available([call_iv, put_iv])
    call_volume = _coerce_int(ce_quote.get("volume")) if ce_quote else None
    put_volume = _coerce_int(pe_quote.get("volume")) if pe_quote else None
    volumes = [volume for volume in (call_volume, put_volume) if volume is not None and volume > 0]
    atm_volume = sum(volumes) if volumes else None
    if atm_iv is None and atm_volume is None:
        return None
    return {
        "symbol": request["symbol"],
        "provider": "kite",
        "live_option_volume": atm_volume,
        "live_option_volume_source": "kite:quote",
        "live_option_volume_kind": "atm_contracts_call_plus_put",
        "live_option_expiry": expiry.isoformat(),
        "live_option_expiry_date": expiry,
        "live_option_strike_count": request["strike_count"],
        "live_option_underlying": spot,
        "live_atm_strike": strike,
        "live_atm_iv": atm_iv,
        "live_atm_call_iv": call_iv,
        "live_atm_put_iv": put_iv,
        "live_atm_iv_source": "kite:quote:calculated-iv" if atm_iv is not None else None,
        "live_atm_call_ltp": _coerce_float(ce_quote.get("last_price")) if ce_quote else None,
        "live_atm_put_ltp": _coerce_float(pe_quote.get("last_price")) if pe_quote else None,
        "live_atm_call_volume": call_volume,
        "live_atm_put_volume": put_volume,
        "live_atm_option_volume": atm_volume,
        "live_atm_call_oi": _coerce_int(ce_quote.get("oi")) if ce_quote else None,
        "live_atm_put_oi": _coerce_int(pe_quote.get("oi")) if pe_quote else None,
    }


def _kite_leg_iv(
    quote: dict[str, Any] | None,
    spot: float,
    strike: float,
    expiry: date,
    trade_date: date,
    risk_free_rate: float,
    option_type: str,
) -> float | None:
    if not quote:
        return None
    price = kite_quote_mid_or_ltp(quote)
    if price is None:
        return None
    iv = implied_volatility_bisection(
        price,
        spot,
        strike,
        years_to_expiry(trade_date, expiry),
        risk_free_rate,
        option_type,
    )
    return iv if iv is not None and 0 < iv <= 2.0 else None


def _kite_option_chain_payload(
    symbol: str,
    spot: float,
    expiry: date,
    rows: list[dict[str, Any]],
    quote_payload: dict[str, Any],
    trade_date: date,
    risk_free_rate: float,
) -> dict[str, Any] | None:
    quotes = quote_payload.get("data") or {}
    by_strike: dict[float, dict[str, Any]] = {}
    for row in rows:
        strike = _coerce_float(row.get("strike"))
        option_type = (row.get("instrument_type") or "").upper()
        key = _kite_instrument_key(row)
        quote = quotes.get(key) if key else None
        if strike is None or option_type not in {"CE", "PE"} or not quote:
            continue
        item = by_strike.setdefault(strike, {"strike": strike})
        item[option_type.lower()] = _kite_chain_leg(
            row,
            quote,
            spot,
            strike,
            expiry,
            trade_date,
            risk_free_rate,
            option_type,
        )
    strikes = [
        item
        for item in by_strike.values()
        if item.get("ce") or item.get("pe")
    ]
    if not strikes:
        return None
    return {
        "symbol": symbol.upper(),
        "expiry": expiry.isoformat(),
        "underlying_last_price": spot,
        "strike_count": len(strikes),
        "strikes": sorted(strikes, key=lambda row: row["strike"]),
        "provider": "kite",
    }


def _kite_chain_leg(
    row: dict[str, Any],
    quote: dict[str, Any],
    spot: float,
    strike: float,
    expiry: date,
    trade_date: date,
    risk_free_rate: float,
    option_type: str,
) -> dict[str, Any]:
    depth = quote.get("depth") or {}
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    return {
        "security_id": row.get("instrument_token"),
        "tradingsymbol": row.get("tradingsymbol"),
        "last_price": _coerce_float(quote.get("last_price")),
        "top_bid_price": _coerce_float((buy[0] or {}).get("price")) if buy else None,
        "top_ask_price": _coerce_float((sell[0] or {}).get("price")) if sell else None,
        "volume": _coerce_int(quote.get("volume")),
        "oi": _coerce_int(quote.get("oi")),
        "previous_oi": None,
        "implied_volatility": _kite_leg_iv(
            quote,
            spot,
            strike,
            expiry,
            trade_date,
            risk_free_rate,
            option_type,
        ),
    }


def _closest_expiry(expiries, trade_date, target_dte: int):
    future = [expiry for expiry in expiries if expiry >= trade_date]
    if not future:
        return None
    return min(future, key=lambda expiry: abs((expiry - trade_date).days - target_dte))


def _dhan_expiry_targets_from_baseline(base: dict[str, Any]) -> list[date]:
    targets = []
    for key in ("expiry_30d", "expiry_60d", "expiry_90d"):
        expiry = _coerce_date(base.get(key))
        if expiry:
            targets.append(expiry)
    return targets


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
        "quote_provider": quote.get("provider"),
        "quote_provider_symbol": quote.get("provider_symbol"),
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
                "call_iv_30",
                "call_iv_60",
                "call_iv_90",
                "put_iv_30",
                "put_iv_60",
                "put_iv_90",
                "fwdv_3060",
                "fwdfct_3060",
                "call_fwdfct_3060",
                "put_fwdfct_3060",
                "fev_30",
                "iv_slope_3060",
            ],
        )
        summary_payload = dict(option_summary)
        if summary_payload.get("provider"):
            payload["live_option_provider"] = summary_payload.pop("provider")
        payload.update(summary_payload)
        payload["avg_option_volume"] = option_summary["live_option_volume"]
        payload["avg_option_volume_source"] = option_summary["live_option_volume_source"]
        payload["avg_option_volume_kind"] = option_summary["live_option_volume_kind"]
        live_metrics = _live_forward_metrics(option_summary, now.date())
        payload.update(live_metrics)
    elif base.get("avg_option_volume") is not None:
        payload["avg_option_volume_source"] = "symbol_daily_metrics"

    if base.get("avg_option_volume") is not None:
        payload.setdefault("avg_option_volume_kind", "eod_total_contracts_all_strikes")
    for key in (
        "iv_30", "iv_60", "iv_90",
        "call_iv_30", "call_iv_60", "call_iv_90",
        "put_iv_30", "put_iv_60", "put_iv_90",
    ):
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
    source = (
        option_summary.get("live_atm_iv_source")
        or option_summary.get("live_option_volume_source")
        or "nse:option-chain-v3"
    )

    iv30 = _constant_maturity_from_terms(terms, 30, "iv")
    iv60 = _constant_maturity_from_terms(terms, 60, "iv")
    iv90 = _constant_maturity_from_terms(terms, 90, "iv")
    call_iv30 = _constant_maturity_from_terms(terms, 30, "call_iv")
    call_iv60 = _constant_maturity_from_terms(terms, 60, "call_iv")
    call_iv90 = _constant_maturity_from_terms(terms, 90, "call_iv")
    put_iv30 = _constant_maturity_from_terms(terms, 30, "put_iv")
    put_iv60 = _constant_maturity_from_terms(terms, 60, "put_iv")
    put_iv90 = _constant_maturity_from_terms(terms, 90, "put_iv")
    fwdv = forward_volatility(iv30, iv60, 30, 60)
    call_fwdv = forward_volatility(call_iv30, call_iv60, 30, 60)
    put_fwdv = forward_volatility(put_iv30, put_iv60, 30, 60)
    metrics: dict[str, Any] = {
        "iv_30": iv30,
        "iv_60": iv60,
        "iv_90": iv90,
        "call_iv_30": call_iv30,
        "call_iv_60": call_iv60,
        "call_iv_90": call_iv90,
        "put_iv_30": put_iv30,
        "put_iv_60": put_iv60,
        "put_iv_90": put_iv90,
        "fwdv_3060": fwdv,
        "fwdfct_3060": forward_factor(iv30, fwdv),
        "call_fwdfct_3060": forward_factor(call_iv30, call_fwdv),
        "put_fwdfct_3060": forward_factor(put_iv30, put_fwdv),
        "fev_30": fwdv,
        "iv_slope_3060": iv_slope(iv30, iv60, 30, 60),
        "iv_term_structure_source": source,
        "forward_analytics_source": source,
        "iv_slope_3060_source": source,
        "live_iv_term_structure": [
            {"tenor": 30, "iv": iv30},
            {"tenor": 60, "iv": iv60},
            {"tenor": 90, "iv": iv90},
        ],
        "live_call_iv_term_structure": [
            {"tenor": 30, "iv": call_iv30},
            {"tenor": 60, "iv": call_iv60},
            {"tenor": 90, "iv": call_iv90},
        ],
        "live_put_iv_term_structure": [
            {"tenor": 30, "iv": put_iv30},
            {"tenor": 60, "iv": put_iv60},
            {"tenor": 90, "iv": put_iv90},
        ],
        "live_raw_iv_term_structure": _raw_term_structure_points(terms, "iv"),
        "live_raw_call_iv_term_structure": _raw_term_structure_points(terms, "call_iv"),
        "live_raw_put_iv_term_structure": _raw_term_structure_points(terms, "put_iv"),
        "expiry_30d": None,
        "expiry_60d": None,
        "expiry_90d": None,
        "dte_30": None,
        "dte_60": None,
        "dte_90": None,
    }
    available_factors = [
        value
        for value in (
            metrics["call_fwdfct_3060"],
            metrics["put_fwdfct_3060"],
        )
        if value is not None
    ]
    metrics["max_fwdfct_3060"] = max(available_factors) if available_factors else None
    for key in (
        "iv_30", "iv_60", "iv_90",
        "call_iv_30", "call_iv_60", "call_iv_90",
        "put_iv_30", "put_iv_60", "put_iv_90",
    ):
        if metrics.get(key) is not None:
            metrics[f"{key}_source"] = source

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
            "call_iv": option_summary.get("live_atm_call_iv"),
            "put_iv": option_summary.get("live_atm_put_iv"),
        }
    ]
    terms = []
    for item in raw_terms:
        expiry_date = _coerce_date(item.get("expiry_date") or item.get("expiry"))
        call_iv_value = _positive_float(item.get("call_iv"))
        put_iv_value = _positive_float(item.get("put_iv"))
        atm_iv_value = (
            (call_iv_value + put_iv_value) / 2
            if call_iv_value is not None and put_iv_value is not None
            else None
        )
        if expiry_date is None or not any(
            value is not None for value in (atm_iv_value, call_iv_value, put_iv_value)
        ):
            continue
        dte = (expiry_date - trade_date).days
        if dte <= 0:
            continue
        terms.append(
            {
                "expiry_date": expiry_date,
                "dte": dte,
                "iv": atm_iv_value,
                "call_iv": call_iv_value,
                "put_iv": put_iv_value,
            }
        )
    return sorted(terms, key=lambda item: (item["dte"], item["expiry_date"]))


def _raw_term_structure_points(terms: list[dict[str, Any]], value_key: str) -> list[dict[str, Any]]:
    points = []
    for item in terms:
        value = item.get(value_key)
        if value is None:
            continue
        points.append(
            {
                "tenor": item["dte"],
                "dte": item["dte"],
                "expiry": item["expiry_date"].isoformat(),
                "iv": value,
            }
        )
    return points


def _constant_maturity_from_terms(
    terms: list[dict[str, Any]], target_dte: int, value_key: str = "iv"
) -> float | None:
    candidates = [item for item in terms if item.get(value_key) is not None]
    exact = next(
        (item[value_key] for item in candidates if item["dte"] == target_dte), None
    )
    if exact is not None:
        return exact
    if len(candidates) == 1:
        return (
            candidates[0][value_key]
            if target_dte <= candidates[0]["dte"]
            else None
        )
    below = [item for item in candidates if item["dte"] < target_dte]
    above = [item for item in candidates if item["dte"] > target_dte]
    if below and above:
        near = max(below, key=lambda item: item["dte"])
        far = min(above, key=lambda item: item["dte"])
        return constant_maturity_iv(
            near[value_key], near["dte"], far[value_key], far["dte"], target_dte
        )
    if above:
        return min(above, key=lambda item: item["dte"])[value_key]
    return None


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


def _coerce_datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    number = _coerce_float(value)
    return int(number) if number is not None else None


def _average_available(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return sum(valid) / len(valid) if valid else None


def _positive_float(value: Any) -> float | None:
    number = _coerce_float(value)
    return number if number is not None and number > 0 else None
