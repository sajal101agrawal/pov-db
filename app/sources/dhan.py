from __future__ import annotations

import asyncio
import csv
from datetime import date
import io
from typing import Any

import httpx

from app.utils.retry import retry_async


class DhanOptionChainClient:
    """Thin DhanHQ v2 option-chain client.

    Dhan requires the underlying security id, not the NSE trading symbol. Keep
    that mapping outside this client so the live layer can support indices,
    equities, and later a broker-neutral instrument master.
    """

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

    async def instrument_map(self, symbols: set[str]) -> dict[str, dict[str, Any]]:
        wanted = {symbol.upper() for symbol in symbols}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await retry_async(
                lambda: client.get(self.scrip_master_url),
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
            )
            response.raise_for_status()
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
            response = await retry_async(
                lambda: client.post(path, json=payload),
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
            )
            response.raise_for_status()
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
