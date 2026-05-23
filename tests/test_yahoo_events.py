from datetime import date

from app.sources.yahoo_events import _normalize_earnings_dates


def test_normalize_earnings_dates_accepts_single_date() -> None:
    assert _normalize_earnings_dates(date(2026, 7, 17)) == [date(2026, 7, 17)]


def test_normalize_earnings_dates_accepts_list() -> None:
    assert _normalize_earnings_dates([date(2026, 5, 23), date(2026, 8, 1)]) == [
        date(2026, 5, 23),
        date(2026, 8, 1),
    ]


def test_normalize_earnings_dates_ignores_non_dates() -> None:
    assert _normalize_earnings_dates(["2026-05-23", None]) == []
