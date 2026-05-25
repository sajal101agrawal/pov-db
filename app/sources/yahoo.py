from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.sources.models import EquityBar
from app.utils.retry import retry_async


INDEX_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^NSEFIN",
    "MIDCPNIFTY": "^NIFMIDCP50",
    "SENSEX": "^BSESN",
}


def yahoo_ticker(symbol: str, explicit: str | None = None) -> str:
    mapped = INDEX_TICKERS.get(symbol.upper())
    if mapped and (not explicit or explicit == f"{symbol.upper()}.NS"):
        return mapped
    if explicit:
        return explicit
    return mapped or f"{symbol.upper()}.NS"


def _to_epoch(day: date) -> int:
    dt = datetime.combine(day, time.min, tzinfo=ZoneInfo("Asia/Kolkata"))
    return int(dt.astimezone(timezone.utc).timestamp())


class YahooFinanceClient:
    chart_base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(
        self,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.75,
        retry_max_delay_seconds: float = 8.0,
    ) -> None:
        self.retry_attempts = retry_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def fetch_equity_history(
        self,
        symbol: str,
        start: date,
        end: date,
        ticker: str | None = None,
    ) -> list[EquityBar]:
        yf_symbol = yahoo_ticker(symbol, ticker)
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
            f"?interval=1d&period1={_to_epoch(start)}&period2={_to_epoch(end)}"
            "&includeAdjustedClose=true&region=IN"
        )
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()

        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        bars: list[EquityBar] = []
        for idx, ts in enumerate(timestamps):
            traded = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
            bars.append(
                EquityBar(
                    symbol=symbol.upper(),
                    trade_date=traded,
                    open=_value_at(quote, "open", idx),
                    high=_value_at(quote, "high", idx),
                    low=_value_at(quote, "low", idx),
                    close=_value_at(quote, "close", idx),
                    volume=int(_value_at(quote, "volume", idx) or 0),
                    source=f"yahoo:{yf_symbol}",
                )
            )
        return [bar for bar in bars if bar.close is not None]

    async def fetch_91d_rate(self, start: date, end: date) -> list[tuple[date, float, str]]:
        bars = await self.fetch_equity_history("^IRX", start, end, ticker="^IRX")
        # Yahoo publishes ^IRX in percent points, e.g. 5.25 means 5.25%.
        return [(bar.trade_date, (bar.close or 0.0) / 100.0, "yahoo:^IRX") for bar in bars]

    async def fetch_live_quotes(
        self,
        symbols: list[str],
        yahoo_symbols: dict[str, str | None] | None = None,
        *,
        concurrency: int = 20,
    ) -> dict[str, dict[str, Any]]:
        yahoo_symbols = yahoo_symbols or {}
        selected = [symbol.upper() for symbol in symbols]
        if not selected:
            return {}

        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        semaphore = asyncio.Semaphore(max(1, concurrency))
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            tasks = [
                self._fetch_live_chart(
                    client,
                    semaphore,
                    symbol,
                    yahoo_ticker(symbol, yahoo_symbols.get(symbol)),
                    headers,
                )
                for symbol in selected
            ]
            rows = await asyncio.gather(*tasks)
        return {row["symbol"]: row for row in rows if row}

    async def _fetch_live_chart(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        symbol: str,
        ticker: str,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        try:
            async with semaphore:
                response = await retry_async(
                    lambda: client.get(
                        f"{self.chart_base_url}/{ticker}",
                        params={"interval": "1m", "range": "1d", "region": "IN"},
                        headers=headers,
                    ),
                    attempts=self.retry_attempts,
                    base_delay_seconds=self.retry_base_delay_seconds,
                    max_delay_seconds=self.retry_max_delay_seconds,
                )
                response.raise_for_status()
            return normalize_live_chart(symbol, ticker, response.json())
        except httpx.HTTPError:
            return None


def normalize_live_chart(symbol: str, ticker: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    current_price = _float(meta.get("regularMarketPrice")) or _last_value(quote, "close")
    if current_price is None:
        return None
    market_time = _int(meta.get("regularMarketTime"))
    if market_time is None and timestamps:
        market_time = _int(timestamps[-1])
    return {
        "symbol": symbol.upper(),
        "provider": "yahoo",
        "provider_symbol": ticker,
        "current_price": current_price,
        "last_price": current_price,
        "open": _first_value(quote, "open"),
        "high": _max_value(quote, "high"),
        "low": _min_value(quote, "low"),
        "close": _float(meta.get("previousClose") or meta.get("chartPreviousClose")),
        "volume": _sum_ints(quote, "volume"),
        "market_state": meta.get("marketState"),
        "regular_market_time": _iso_from_epoch(market_time),
    }


def _value_at(mapping: dict, key: str, idx: int) -> float | None:
    values = mapping.get(key) or []
    if idx >= len(values):
        return None
    value = values[idx]
    return float(value) if value is not None else None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    parsed = _float(value)
    return int(parsed) if parsed is not None else None


def _iso_from_epoch(value: int | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .astimezone(ZoneInfo("Asia/Kolkata"))
        .isoformat()
    )


def _values(mapping: dict, key: str) -> list[float]:
    return [_float(value) for value in mapping.get(key, []) if _float(value) is not None]


def _first_value(mapping: dict, key: str) -> float | None:
    values = _values(mapping, key)
    return values[0] if values else None


def _last_value(mapping: dict, key: str) -> float | None:
    values = _values(mapping, key)
    return values[-1] if values else None


def _max_value(mapping: dict, key: str) -> float | None:
    values = _values(mapping, key)
    return max(values) if values else None


def _min_value(mapping: dict, key: str) -> float | None:
    values = _values(mapping, key)
    return min(values) if values else None


def _sum_ints(mapping: dict, key: str) -> int | None:
    values = [_int(value) for value in mapping.get(key, []) if _int(value) is not None]
    return sum(values) if values else None
