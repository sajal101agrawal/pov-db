from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

from app.sources.yahoo import yahoo_ticker


class YahooEarningsCalendarClient:
    """Fetch forward-looking earnings dates from Yahoo Finance.

    Only the ``Earnings Date`` calendar field is used so dividend, AGM, and other
    corporate-action dates are excluded.
    """

    def __init__(self, request_delay_seconds: float = 0.1) -> None:
        self.request_delay_seconds = request_delay_seconds

    async def fetch_upcoming_result_events(
        self,
        symbols: list[str],
        yahoo_symbols: dict[str, str | None] | None = None,
        *,
        min_event_date: date | None = None,
    ) -> list[dict[str, Any]]:
        min_date = min_event_date or date.today()
        events: list[dict[str, Any]] = []
        yahoo_symbols = yahoo_symbols or {}
        for symbol in symbols:
            events.extend(
                await asyncio.to_thread(
                    self._fetch_symbol,
                    symbol.upper(),
                    yahoo_symbols.get(symbol.upper()),
                    min_date,
                )
            )
            if self.request_delay_seconds:
                await asyncio.sleep(self.request_delay_seconds)
        return events

    def _fetch_symbol(
        self,
        symbol: str,
        explicit_yahoo_symbol: str | None,
        min_event_date: date,
    ) -> list[dict[str, Any]]:
        import yfinance as yf

        ticker = yf.Ticker(yahoo_ticker(symbol, explicit_yahoo_symbol))
        calendar = ticker.calendar
        if not isinstance(calendar, dict):
            return []

        earnings_dates = _normalize_earnings_dates(calendar.get("Earnings Date"))
        events: list[dict[str, Any]] = []
        for event_date in earnings_dates:
            if event_date < min_event_date:
                continue
            events.append(
                {
                    "symbol": symbol,
                    "event_date": event_date,
                    "event_type": "RESULT",
                    "description": "Scheduled earnings date (Yahoo Finance)",
                    "source": "yahoo:earnings-calendar",
                }
            )
        return events


def _normalize_earnings_dates(raw: Any) -> list[date]:
    if raw is None:
        return []
    if isinstance(raw, datetime):
        return [raw.date()]
    if isinstance(raw, date):
        return [raw]
    if isinstance(raw, list):
        result: list[date] = []
        for value in raw:
            if isinstance(value, datetime):
                result.append(value.date())
            elif isinstance(value, date):
                result.append(value)
        return result
    return []
