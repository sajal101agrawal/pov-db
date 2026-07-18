from __future__ import annotations

import asyncio
from datetime import date, timedelta

from app.api.routes import (
    _live_payloads_by_symbol,
    _matches_numeric_filters,
    _overlay_live_dashboard_payload,
    _overlay_live_term_structure,
    _refresh_current_forward_factor_percentiles,
)


def test_dashboard_live_overlay_allows_scanner_filters_to_use_displayed_values() -> None:
    eod_payload = {
        "symbol": "ABC",
        "current_price": 190,
        "avg_option_volume": 5000,
        "option_volume_60d": 2500,
        "fwdfct_3060": 0.12,
        "iv_slope_3060": 0.02,
    }
    live_by_symbol = {
        "ABC": {
            "symbol": "ABC",
            "current_price": 250,
            "avg_option_volume": 7200,
            "option_volume_60d": 3200,
            "fwdfct_3060": 0.18,
            "iv_slope_3060": -0.01,
        }
    }
    filters = {
        "current_price": {"min": 200},
        "avg_option_volume": {"min": 7000},
        "option_volume_60d": {"min": 3000},
        "fwdfct_3060": {"min": 0.16},
        "iv_slope_3060": {"max": 0.000001},
    }

    assert not _matches_numeric_filters(eod_payload, filters)

    overlaid = _overlay_live_dashboard_payload(eod_payload, live_by_symbol)

    assert _matches_numeric_filters(overlaid, filters)


def test_golden_strategy_filter_uses_call_or_put_forward_factor() -> None:
    filters = {"max_fwdfct_3060": {"min": 0.16}}

    assert _matches_numeric_filters(
        {"call_fwdfct_3060": 0.12, "put_fwdfct_3060": 0.18, "max_fwdfct_3060": 0.18},
        filters,
    )
    assert not _matches_numeric_filters(
        {"call_fwdfct_3060": 0.12, "put_fwdfct_3060": 0.15, "max_fwdfct_3060": 0.15},
        filters,
    )
    assert _matches_numeric_filters(
        {"call_fwdfct_3060": 0.18, "put_fwdfct_3060": 0.12, "max_fwdfct_3060": 0.12},
        filters,
    )
    assert not _matches_numeric_filters(
        {"call_fwdfct_3060": 0.12, "put_fwdfct_3060": 0.15, "max_fwdfct_3060": 0.18},
        filters,
    )


def test_bid_ask_spread_filter_uses_percent_points() -> None:
    filters = {"bid_ask_spread_pct": {"max": 5}}

    assert _matches_numeric_filters({"bid_ask_spread_pct": 4.99, "bid_ask_spread_dte": 60}, filters)
    assert _matches_numeric_filters(
        {
            "bid_ask_spread_pct": 2.0,
            "bid_ask_spread_dte": 60,
            "live_60d_atm_call_bid_ask_spread_pct": 10.0,
            "live_60d_atm_put_bid_ask_spread_pct": 2.0,
        },
        filters,
    )
    assert not _matches_numeric_filters({"bid_ask_spread_pct": 5.0, "bid_ask_spread_dte": 60}, filters)
    assert not _matches_numeric_filters({"bid_ask_spread_pct": 5.01, "bid_ask_spread_dte": 60}, filters)
    assert _matches_numeric_filters({}, filters)
    assert _matches_numeric_filters({"bid_ask_spread_pct": 5.01}, filters)


def test_dashboard_post_market_live_db_fallback_keeps_spread_filter() -> None:
    class FakeCache:
        async def get_live_symbols(self) -> list[dict]:
            return []

    class FakeRepo:
        async def latest_live_metrics(self, symbols: list[str] | None = None) -> dict[str, dict]:
            assert symbols is None
            return {
                "ABC": {
                    "symbol": "ABC",
                    "bid_ask_spread_pct": 4.5,
                    "bid_ask_spread_dte": 56,
                    "snapshot_time": "2026-07-09T15:59:00+05:30",
                }
            }

    live_by_symbol = asyncio.run(
        _live_payloads_by_symbol(
            FakeCache(),  # type: ignore[arg-type]
            FakeRepo(),  # type: ignore[arg-type]
        )
    )
    payload = _overlay_live_dashboard_payload({"symbol": "ABC"}, live_by_symbol)

    assert _matches_numeric_filters(payload, {"bid_ask_spread_pct": {"max": 5}})


def test_term_structure_live_overlay_uses_snapshot_date() -> None:
    result = {
        "symbol": "ABC",
        "current": {"trade_date": "2026-06-30", "iv_30": 0.20},
        "history": [{"trade_date": "2026-06-30", "iv_30": 0.20}],
    }
    live = {
        "snapshot_time": "2026-07-01T10:11:02.975027+05:30",
        "iv_term_structure_source": "kite:quote:calculated-iv",
        "iv_30": 0.22,
        "iv_60": 0.21,
        "fwdfct_3060": 0.10,
    }

    overlaid = _overlay_live_term_structure(result, live)

    assert overlaid["current"]["trade_date"] == "2026-07-01"
    assert overlaid["current"]["is_live"] is True
    assert overlaid["current"]["iv_30"] == 0.22
    assert [item["trade_date"] for item in overlaid["history"]] == [
        "2026-06-30",
        "2026-07-01",
    ]


def test_term_structure_live_overlay_preserves_signal_values_when_live_is_partial() -> None:
    result = {
        "symbol": "COALINDIA",
        "current": {
            "trade_date": "2026-07-10",
            "call_iv_30": 0.19,
            "call_iv_60": 0.16,
            "call_fwdfct_3060": 0.50,
            "call_fwdfct_3060_percentile": 92.1,
            "put_fwdfct_3060": -0.26,
            "iv_90": 0.30,
        },
        "history": [
            {
                "trade_date": "2026-07-10",
                "call_iv_30": 0.19,
                "call_iv_60": 0.16,
                "call_fwdfct_3060": 0.50,
                "put_fwdfct_3060": -0.26,
                "iv_90": 0.30,
            }
        ],
    }
    live = {
        "snapshot_time": "2026-07-13T10:11:02.975027+05:30",
        "iv_term_structure_source": "kite:quote:calculated-iv",
        "call_iv_30": None,
        "call_iv_60": 0.17,
        "call_fwdfct_3060": None,
        "put_fwdfct_3060": -0.30,
        "iv_90": None,
    }

    overlaid = _overlay_live_term_structure(result, live)
    current = overlaid["current"]

    assert current["trade_date"] == "2026-07-13"
    assert current["call_iv_30"] == 0.19
    assert current["call_iv_60"] == 0.17
    assert current["call_fwdfct_3060"] == 0.50
    assert current["put_fwdfct_3060"] == -0.30
    assert current["iv_90"] is None


def test_live_percentile_refresh_includes_side_specific_iv_terms() -> None:
    history = [
        {
            "trade_date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "call_iv_30": 0.10 + index / 1000,
            "call_iv_60": 0.40 - index / 1000,
            "put_iv_30": 0.20 + index / 1000,
            "put_iv_60": 0.30 - index / 1000,
            "call_slope_3060": -0.020 + index / 10000,
            "put_slope_3060": -0.010 + index / 10000,
        }
        for index in range(60)
    ]
    current = {
        "trade_date": "2026-04-01",
        "call_iv_30": 0.159,
        "call_iv_60": 0.341,
        "put_iv_30": 0.230,
        "put_iv_60": 0.270,
        "call_slope_3060": -0.018,
        "put_slope_3060": -0.005,
    }

    _refresh_current_forward_factor_percentiles(history, current)

    assert current["call_iv_30_percentile"] == 100.0
    assert current["call_iv_60_percentile"] == 1.67
    assert current["put_iv_30_percentile"] == 51.67
    assert current["put_iv_60_percentile"] == 50.0
    assert current["call_slope_3060_percentile"] == 35.0
    assert current["put_slope_3060_percentile"] == 85.0


def test_live_percentile_refresh_requires_sixty_historical_values() -> None:
    history = [
        {
            "trade_date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "call_iv_30": 0.10 + index / 1000,
        }
        for index in range(59)
    ]
    current = {"trade_date": "2026-04-01", "call_iv_30": 0.20}

    _refresh_current_forward_factor_percentiles(history, current)

    assert "call_iv_30_percentile" not in current
