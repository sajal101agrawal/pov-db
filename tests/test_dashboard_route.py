from __future__ import annotations

from app.api.routes import (
    _matches_numeric_filters,
    _overlay_live_dashboard_payload,
    _overlay_live_term_structure,
)


def test_dashboard_live_overlay_allows_scanner_filters_to_use_displayed_values() -> None:
    eod_payload = {
        "symbol": "ABC",
        "current_price": 190,
        "avg_option_volume": 5000,
        "fwdfct_3060": 0.12,
        "iv_slope_3060": 0.02,
    }
    live_by_symbol = {
        "ABC": {
            "symbol": "ABC",
            "current_price": 250,
            "avg_option_volume": 7200,
            "fwdfct_3060": 0.18,
            "iv_slope_3060": -0.01,
        }
    }
    filters = {
        "current_price": {"min": 200},
        "avg_option_volume": {"min": 7000},
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
