from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import httpx

from app.sources.models import EquityBar


INDEX_TICKERS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^NSEFIN",
    "MIDCPNIFTY": "^NIFMIDCP50",
    "SENSEX": "^BSESN",
}


def yahoo_ticker(symbol: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return INDEX_TICKERS.get(symbol.upper(), f"{symbol.upper()}.NS")


def _to_epoch(day: date) -> int:
    dt = datetime.combine(day, time.min, tzinfo=ZoneInfo("Asia/Kolkata"))
    return int(dt.astimezone(timezone.utc).timestamp())


class YahooFinanceClient:
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


def _value_at(mapping: dict, key: str, idx: int) -> float | None:
    values = mapping.get(key) or []
    if idx >= len(values):
        return None
    value = values[idx]
    return float(value) if value is not None else None
