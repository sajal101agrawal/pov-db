from __future__ import annotations

from app.api.routes import _matches_numeric_filters, _overlay_live_dashboard_payload


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
