from __future__ import annotations

import asyncio
import base64
import csv
from datetime import date
from datetime import datetime
import hashlib
import hmac
import io
import time
from typing import Any

import httpx

from app.utils.retry import retry_async


class DhanOptionChainClient:
    """Thin DhanHQ v2 option-chain client.

    Dhan requires the underlying security id, not the NSE trading symbol. Keep
    that mapping outside this client so the live layer can support indices,
    equities, and later a broker-neutral instrument master.
    """

    auth_base_url = "https://auth.dhan.co"
    base_url = "https://api.dhan.co/v2"
    scrip_master_url = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

    def __init__(
        self,
        client_id: str,
        access_token: str,
        min_interval_seconds: float = 3.0,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self.min_interval_seconds = min_interval_seconds
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def expiry_list(self, underlying_scrip: int, underlying_seg: str) -> list[date]:
        payload = await self._post(
            "/optionchain/expirylist",
            {"UnderlyingScrip": underlying_scrip, "UnderlyingSeg": underlying_seg},
        )
        return [date.fromisoformat(item) for item in payload.get("data", [])]

    async def option_chain(self, underlying_scrip: int, underlying_seg: str, expiry: date) -> dict[str, Any]:
        return await self._post(
            "/optionchain",
            {
                "UnderlyingScrip": underlying_scrip,
                "UnderlyingSeg": underlying_seg,
                "Expiry": expiry.isoformat(),
            },
        )

    async def market_quote(self, instruments: dict[str, list[int]]) -> dict[str, Any]:
        return await self._post("/marketfeed/quote", instruments)

    @classmethod
    async def generate_access_token(
        cls,
        client_id: str,
        pin: str,
        totp_secret: str,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=cls.auth_base_url, timeout=15) as client:
            async def request() -> httpx.Response:
                response = await client.post(
                    "/app/generateAccessToken",
                    params={
                        "dhanClientId": client_id,
                        "pin": pin,
                        "totp": current_totp(totp_secret),
                    },
                )
                response.raise_for_status()
                return response

            response = await retry_async(
                request,
                attempts=retry_attempts,
                base_delay_seconds=retry_base_delay_seconds,
                max_delay_seconds=retry_max_delay_seconds,
                retryable=_is_retryable_http_exception,
            )
        payload = response.json()
        if not payload.get("accessToken"):
            raise RuntimeError("Dhan token response did not include accessToken")
        return payload

    async def instrument_map(self, symbols: set[str]) -> dict[str, dict[str, Any]]:
        wanted = {symbol.upper() for symbol in symbols}
        async with httpx.AsyncClient(timeout=45) as client:
            async def request() -> httpx.Response:
                response = await client.get(self.scrip_master_url)
                response.raise_for_status()
                return response

            response = await retry_async(
                request,
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
                retryable=_is_retryable_http_exception,
            )
        output: dict[str, dict[str, Any]] = {}
        reader = csv.DictReader(io.StringIO(response.text))
        for row in reader:
            exch = row.get("EXCH_ID")
            segment = row.get("SEGMENT")
            underlying = (row.get("UNDERLYING_SYMBOL") or row.get("SYMBOL_NAME") or "").upper()
            instrument = row.get("INSTRUMENT")
            if underlying not in wanted:
                continue
            if underlying in output:
                continue
            if exch == "NSE" and segment == "D" and instrument in {"OPTSTK", "OPTIDX", "FUTSTK", "FUTIDX"}:
                underlying_id = row.get("UNDERLYING_SECURITY_ID") or row.get("SECURITY_ID")
                if not underlying_id or underlying_id in {"0", "NA"}:
                    continue
                output[underlying] = {
                    "underlying_scrip": int(float(underlying_id)),
                    "underlying_seg": "IDX_I" if instrument in {"OPTIDX", "FUTIDX"} else "NSE_EQ",
                    "source": "dhan:scrip-master-detailed",
                }
        return output

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self.min_interval_seconds)
        headers = {
            "Content-Type": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        }
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
            async def request() -> httpx.Response:
                response = await client.post(path, json=payload)
                response.raise_for_status()
                return response

            response = await retry_async(
                request,
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
                retryable=_is_retryable_http_exception,
            )
            return response.json()


def normalize_option_chain(symbol: str, expiry: date, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    oc = data.get("oc") or {}
    strikes = []
    for strike_text, legs in oc.items():
        strike = _float(strike_text)
        item = {"strike": strike}
        for side in ("ce", "pe"):
            leg = legs.get(side) or {}
            greeks = leg.get("greeks") or {}
            item[side] = {
                "security_id": leg.get("security_id"),
                "last_price": _float(leg.get("last_price")),
                "top_bid_price": _float(leg.get("top_bid_price")),
                "top_ask_price": _float(leg.get("top_ask_price")),
                "volume": _int(leg.get("volume")),
                "oi": _int(leg.get("oi")),
                "previous_oi": _int(leg.get("previous_oi")),
                "implied_volatility": _iv_decimal(leg.get("implied_volatility")),
                "delta": _float(greeks.get("delta")),
                "theta": _float(greeks.get("theta")),
                "gamma": _float(greeks.get("gamma")),
                "vega": _float(greeks.get("vega")),
            }
        strikes.append(item)
    return {
        "symbol": symbol.upper(),
        "expiry": expiry.isoformat(),
        "underlying_last_price": _float(data.get("last_price")),
        "strike_count": len(strikes),
        "strikes": sorted(strikes, key=lambda row: row["strike"] or 0),
        "provider": "dhan",
    }


def normalize_option_chain_summary(
    symbol: str,
    expiry: date,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    data = payload.get("data") or {}
    oc = data.get("oc") or {}
    if not oc:
        return None

    underlying = _float(data.get("last_price"))
    total_volume = 0
    volume_leg_count = 0
    strikes: list[dict[str, Any]] = []
    for strike_text, legs in oc.items():
        strike = _float(strike_text)
        if strike is None:
            continue
        ce = legs.get("ce") or {}
        pe = legs.get("pe") or {}
        ce_volume = _int(ce.get("volume"))
        pe_volume = _int(pe.get("volume"))
        for volume in (ce_volume, pe_volume):
            if volume is not None:
                total_volume += volume
                volume_leg_count += 1
        strikes.append(
            {
                "strike": strike,
                "ce_iv": _iv_decimal(ce.get("implied_volatility")),
                "pe_iv": _iv_decimal(pe.get("implied_volatility")),
                "ce_ltp": _float(ce.get("last_price")),
                "pe_ltp": _float(pe.get("last_price")),
                "ce_volume": ce_volume,
                "pe_volume": pe_volume,
                "ce_oi": _int(ce.get("oi")),
                "pe_oi": _int(pe.get("oi")),
            }
        )

    if not strikes or volume_leg_count == 0:
        return None

    atm = _atm_row(strikes, underlying)
    call_iv = atm.get("ce_iv") if atm else None
    put_iv = atm.get("pe_iv") if atm else None
    atm_iv = _average_available([call_iv, put_iv])
    call_volume = atm.get("ce_volume") if atm else None
    put_volume = atm.get("pe_volume") if atm else None
    atm_volumes = [volume for volume in (call_volume, put_volume) if volume is not None]
    atm_volume = sum(atm_volumes) if atm_volumes else None

    return {
        "symbol": symbol.upper(),
        "provider": "dhan",
        "live_option_volume": total_volume,
        "live_option_volume_source": "dhan:optionchain",
        "live_option_volume_kind": "total_contracts_all_strikes",
        "live_option_expiry": expiry.isoformat(),
        "live_option_expiry_date": expiry,
        "live_option_strike_count": len(strikes),
        "live_option_underlying": underlying,
        "live_atm_strike": atm.get("strike") if atm else None,
        "live_atm_iv": atm_iv,
        "live_atm_call_iv": call_iv,
        "live_atm_put_iv": put_iv,
        "live_atm_iv_source": "dhan:optionchain" if atm_iv is not None else None,
        "live_atm_call_ltp": atm.get("ce_ltp") if atm else None,
        "live_atm_put_ltp": atm.get("pe_ltp") if atm else None,
        "live_atm_call_volume": call_volume,
        "live_atm_put_volume": put_volume,
        "live_atm_option_volume": atm_volume,
        "live_atm_call_oi": atm.get("ce_oi") if atm else None,
        "live_atm_put_oi": atm.get("pe_oi") if atm else None,
    }


def combine_expiry_summaries(
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
            "call_iv": item.get("live_atm_call_iv"),
            "put_iv": item.get("live_atm_put_iv"),
            "underlying": item.get("live_option_underlying"),
            "strike_count": item.get("live_option_strike_count"),
            "call_volume": item.get("live_atm_call_volume"),
            "put_volume": item.get("live_atm_put_volume"),
            "option_volume": item.get("live_atm_option_volume"),
        }
        for item in summaries
    ]
    primary["live_iv_term_count"] = len(summaries)
    primary["symbol"] = symbol.upper()
    return primary


def normalize_market_quotes(
    instrument_to_symbol: dict[tuple[str, int], str],
    payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    data = payload.get("data") or {}
    output: dict[str, dict[str, Any]] = {}
    for segment, rows in data.items():
        if not isinstance(rows, dict):
            continue
        for security_id_text, raw in rows.items():
            security_id = _int(security_id_text)
            if security_id is None:
                continue
            symbol = instrument_to_symbol.get((segment, security_id))
            if not symbol:
                continue
            ohlc = raw.get("ohlc") or {}
            output[symbol] = {
                "symbol": symbol,
                "provider": "dhan",
                "current_price": _float(raw.get("last_price")),
                "last_price": _float(raw.get("last_price")),
                "open": _float(ohlc.get("open")),
                "high": _float(ohlc.get("high")),
                "low": _float(ohlc.get("low")),
                "close": _float(ohlc.get("close")),
                "volume": _int(raw.get("volume")),
                "oi": _int(raw.get("oi")),
            }
    return output


def current_totp(secret: str, *, timestamp: int | None = None, interval: int = 30) -> str:
    cleaned = secret.replace(" ", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    key = base64.b32decode(cleaned + padding)
    counter = int((timestamp if timestamp is not None else time.time()) // interval)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def token_expiry(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("expiryTime")
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _is_retryable_http_exception(exc: Exception) -> bool:
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


def _atm_row(rows: list[dict[str, Any]], underlying: float | None) -> dict[str, Any] | None:
    if not rows or underlying is None:
        return None
    return min(rows, key=lambda row: (abs(row["strike"] - underlying), row["strike"]))


def _average_available(values: list[float | None]) -> float | None:
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
    if parsed is None:
        return None
    return parsed / 100.0 if parsed > 2 else parsed
