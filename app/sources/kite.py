from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
from typing import Any

import httpx

from app.utils.retry import retry_async


class KiteConnectClient:
    base_url = "https://api.kite.trade"
    login_url = "https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"

    def __init__(
        self,
        api_key: str,
        access_token: str | None = None,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    @classmethod
    async def generate_session(
        cls,
        api_key: str,
        api_secret: str,
        request_token: str,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> dict[str, Any]:
        checksum = session_checksum(api_key, request_token, api_secret)
        async with httpx.AsyncClient(base_url=cls.base_url, timeout=15) as client:
            async def request() -> httpx.Response:
                response = await client.post(
                    "/session/token",
                    headers={"X-Kite-Version": "3"},
                    data={
                        "api_key": api_key,
                        "request_token": request_token,
                        "checksum": checksum,
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
        data = payload.get("data") or {}
        if not data.get("access_token"):
            raise RuntimeError("Kite session response did not include access_token")
        return data

    async def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        path = f"/instruments/{exchange}" if exchange else "/instruments"
        response = await self._get(path)
        reader = csv.DictReader(io.StringIO(response.text))
        return [dict(row) for row in reader]

    async def quote(self, instruments: list[str]) -> dict[str, Any]:
        if not instruments:
            return {"status": "success", "data": {}}
        response = await self._get(
            "/quote",
            params=[("i", instrument) for instrument in instruments],
        )
        return response.json()

    async def _get(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
    ) -> httpx.Response:
        headers = {"X-Kite-Version": "3"}
        if self.access_token:
            headers["Authorization"] = f"token {self.api_key}:{self.access_token}"
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
            async def request() -> httpx.Response:
                response = await client.get(path, params=params)
                response.raise_for_status()
                return response

            return await retry_async(
                request,
                attempts=self.retry_attempts,
                base_delay_seconds=self.retry_base_delay_seconds,
                max_delay_seconds=self.retry_max_delay_seconds,
                retryable=_is_retryable_http_exception,
            )


def session_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()


def login_url(api_key: str) -> str:
    return KiteConnectClient.login_url.format(api_key=api_key)


def normalize_market_quotes(payload: dict[str, Any], symbol_to_key: dict[str, str]) -> dict[str, dict[str, Any]]:
    data = payload.get("data") or {}
    output: dict[str, dict[str, Any]] = {}
    for symbol, key in symbol_to_key.items():
        raw = data.get(key)
        if not raw:
            continue
        ohlc = raw.get("ohlc") or {}
        output[symbol] = {
            "symbol": symbol,
            "provider": "kite",
            "provider_symbol": key,
            "instrument_token": _int(raw.get("instrument_token")),
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


def quote_last_price(payload: dict[str, Any], instrument_key: str) -> float | None:
    return _float(((payload.get("data") or {}).get(instrument_key) or {}).get("last_price"))


def quote_mid_or_ltp(raw: dict[str, Any]) -> float | None:
    depth = raw.get("depth") or {}
    buy = depth.get("buy") or []
    sell = depth.get("sell") or []
    bid = _float((buy[0] or {}).get("price")) if buy else None
    ask = _float((sell[0] or {}).get("price")) if sell else None
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    return _float(raw.get("last_price"))


def token_login_time(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("login_time")
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
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


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    parsed = _float(value)
    return int(parsed) if parsed is not None else None
