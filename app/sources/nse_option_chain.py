from __future__ import annotations

import asyncio
from datetime import date, datetime
import time
from typing import Any

import httpx

from app.sources.nse import NSE_HEADERS
from app.utils.retry import retry_async


INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


class NSEOptionChainClient:
    base_url = "https://www.nseindia.com/api/option-chain-v3"
    discovery_expiry = "01-Jan-2099"

    def __init__(
        self,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
        concurrency: int = 2,
        min_interval_seconds: float = 0.25,
    ) -> None:
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.concurrency = max(1, concurrency)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def fetch_summaries(
        self,
        symbols: list[str],
        expiry_hints: dict[str, date | str | None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        expiry_hints = expiry_hints or {}
        headers = {
            **NSE_HEADERS,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.nseindia.com/option-chain",
        }
        semaphore = asyncio.Semaphore(self.concurrency)
        stop_event = asyncio.Event()
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
            tasks = [
                self._fetch_summary(
                    client,
                    semaphore,
                    stop_event,
                    symbol.upper(),
                    expiry_hints.get(symbol.upper()),
                )
                for symbol in symbols
            ]
            rows = await asyncio.gather(*tasks)
        return {row["symbol"]: row for row in rows if row}

    async def _fetch_summary(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        stop_event: asyncio.Event,
        symbol: str,
        expiry_hint: date | str | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if stop_event.is_set():
            return None

        expiry_targets = _expiry_targets(expiry_hint)
        if expiry_targets:
            summaries = []
            for expiry in expiry_targets:
                payload = await self._fetch_payload(client, semaphore, stop_event, symbol, expiry)
                summary = normalize_option_chain_summary(symbol, payload, expiry)
                if summary:
                    summaries.append(summary)
                if stop_event.is_set():
                    break
            return _combine_expiry_summaries(symbol, summaries)

        expiry = self.discovery_expiry
        payload = await self._fetch_payload(client, semaphore, stop_event, symbol, expiry)
        summary = normalize_option_chain_summary(symbol, payload, expiry)
        if summary:
            return summary

        expiries = ((payload or {}).get("records") or {}).get("expiryDates") or []
        if not expiries or stop_event.is_set():
            return None
        summaries = []
        for expiry in [str(item) for item in expiries[:3]]:
            payload = await self._fetch_payload(client, semaphore, stop_event, symbol, expiry)
            summary = normalize_option_chain_summary(symbol, payload, expiry)
            if summary:
                summaries.append(summary)
            if stop_event.is_set():
                break
        return _combine_expiry_summaries(symbol, summaries)

    async def _fetch_payload(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        stop_event: asyncio.Event,
        symbol: str,
        expiry: str,
    ) -> dict[str, Any] | None:
        if stop_event.is_set():
            return None

        async def request() -> httpx.Response:
            await self._throttle()
            response = await client.get(
                self.base_url,
                params={
                    "type": "Indices" if symbol in INDEX_SYMBOLS else "Equity",
                    "symbol": symbol,
                    "expiry": expiry,
                },
            )
            response.raise_for_status()
            return response

        try:
            async with semaphore:
                response = await retry_async(
                    request,
                    attempts=self.retry_attempts,
                    base_delay_seconds=self.retry_base_delay_seconds,
                    max_delay_seconds=self.retry_max_delay_seconds,
                    retryable=_is_retryable_nse_exception,
                )
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 429}:
                stop_event.set()
            return None
        except (httpx.HTTPError, ValueError):
            return None

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            delay = self.min_interval_seconds - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()


def normalize_option_chain_summary(
    symbol: str,
    payload: dict[str, Any] | None,
    expiry: str,
) -> dict[str, Any] | None:
    records = (payload or {}).get("records") or {}
    rows = records.get("data") or []
    if not rows:
        return None

    total_volume = 0
    volume_leg_count = 0
    strike_count = 0
    underlying = _float(records.get("underlyingValue"))
    if not underlying:
        underlying = _first_underlying(rows)

    strikes: list[dict[str, Any]] = []
    for row in rows:
        strike = _float(row.get("strikePrice"))
        if strike is None:
            continue
        strike_count += 1
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        for leg in (ce, pe):
            volume = _int(leg.get("totalTradedVolume"))
            if volume is not None:
                total_volume += volume
                volume_leg_count += 1
        strikes.append(
            {
                "strike": strike,
                "ce_iv": _iv_decimal(ce.get("impliedVolatility")),
                "pe_iv": _iv_decimal(pe.get("impliedVolatility")),
            }
        )

    if volume_leg_count == 0:
        return None

    atm = _atm_row(strikes, underlying)
    atm_iv = _average([atm.get("ce_iv"), atm.get("pe_iv")]) if atm else None
    expiry_date = _parse_expiry(expiry)
    return {
        "symbol": symbol.upper(),
        "provider": "nse",
        "live_option_volume": total_volume,
        "live_option_volume_source": "nse:option-chain-v3",
        "live_option_volume_kind": "total_contracts_all_strikes",
        "live_option_expiry": expiry,
        "live_option_expiry_date": expiry_date,
        "live_option_strike_count": strike_count,
        "live_option_underlying": underlying,
        "live_atm_strike": atm.get("strike") if atm else None,
        "live_atm_iv": atm_iv,
        "live_atm_iv_source": "nse:option-chain-v3" if atm_iv is not None else None,
        "nse_option_chain_timestamp": records.get("timestamp"),
    }


def _is_retryable_nse_exception(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ),
    )


def _format_expiry(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.strftime("%d-%b-%Y")
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date().strftime("%d-%b-%Y")
    except ValueError:
        return text


def _parse_expiry(value: str) -> date | None:
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _expiry_targets(value: date | str | dict[str, Any] | None) -> list[str]:
    if not isinstance(value, dict):
        formatted = _format_expiry(value)
        return [formatted] if formatted else []
    targets = [
        _format_expiry(value.get("expiry_30d")),
        _format_expiry(value.get("expiry_60d")),
        _format_expiry(value.get("expiry_90d")),
    ]
    seen = set()
    result = []
    for target in targets:
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


def _combine_expiry_summaries(
    symbol: str,
    summaries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not summaries:
        return None
    primary = dict(summaries[0])
    primary["live_iv_terms"] = [
        {
            "expiry": item.get("live_option_expiry"),
            "expiry_date": item.get("live_option_expiry_date"),
            "atm_strike": item.get("live_atm_strike"),
            "atm_iv": item.get("live_atm_iv"),
            "underlying": item.get("live_option_underlying"),
            "strike_count": item.get("live_option_strike_count"),
            "timestamp": item.get("nse_option_chain_timestamp"),
        }
        for item in summaries
    ]
    primary["live_iv_term_count"] = len(summaries)
    primary["symbol"] = symbol.upper()
    return primary


def _first_underlying(rows: list[dict[str, Any]]) -> float | None:
    for row in rows:
        for side in ("CE", "PE"):
            value = _float((row.get(side) or {}).get("underlyingValue"))
            if value:
                return value
    return None


def _atm_row(rows: list[dict[str, Any]], underlying: float | None) -> dict[str, Any] | None:
    if not rows or underlying is None:
        return None
    return min(rows, key=lambda row: (abs(row["strike"] - underlying), row["strike"]))


def _average(values: list[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return sum(valid) / len(valid) if valid else None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    parsed = _float(value)
    return int(parsed) if parsed is not None else None


def _iv_decimal(value: Any) -> float | None:
    parsed = _float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed / 100.0 if parsed > 2 else parsed
