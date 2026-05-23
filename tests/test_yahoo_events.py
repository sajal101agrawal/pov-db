import asyncio
from datetime import date

from app.sources.yahoo_events import YahooEarningsCalendarClient, _normalize_earnings_dates


def test_normalize_earnings_dates_accepts_single_date() -> None:
    assert _normalize_earnings_dates(date(2026, 7, 17)) == [date(2026, 7, 17)]


def test_normalize_earnings_dates_accepts_list() -> None:
    assert _normalize_earnings_dates([date(2026, 5, 23), date(2026, 8, 1)]) == [
        date(2026, 5, 23),
        date(2026, 8, 1),
    ]


def test_normalize_earnings_dates_ignores_non_dates() -> None:
    assert _normalize_earnings_dates(["2026-05-23", None]) == []


def test_fetch_upcoming_result_events_retries_transient_failures() -> None:
    class FlakyYahooClient(YahooEarningsCalendarClient):
        def __init__(self) -> None:
            super().__init__(request_delay_seconds=0, retry_attempts=2)
            self.calls = 0

        def _fetch_symbol(self, symbol: str, explicit_yahoo_symbol: str | None, min_event_date: date):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary yahoo failure")
            return [
                {
                    "symbol": symbol,
                    "event_date": min_event_date,
                    "event_type": "RESULT",
                    "description": "Scheduled earnings date (Yahoo Finance)",
                    "source": "yahoo:earnings-calendar",
                }
            ]

    client = FlakyYahooClient()
    events = asyncio.run(
        client.fetch_upcoming_result_events(["RELIANCE"], min_event_date=date(2026, 7, 17))
    )

    assert client.calls == 2
    assert events[0]["symbol"] == "RELIANCE"
