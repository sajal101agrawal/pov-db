from __future__ import annotations

from datetime import date

from app.api.routes import _overlay_live_volatility_cone, _volatility_cone_window


def test_volatility_cone_window_uses_current_iv_reference_for_term_tenors() -> None:
    stats = {
        "rv60_min": 0.10,
        "rv60_p10": 0.12,
        "rv60_p25": 0.14,
        "rv60_median": 0.20,
        "rv60_p75": 0.25,
        "rv60_p90": 0.30,
        "rv60_max": 0.50,
    }
    latest = {
        "rv_60": 0.3543,
        "iv_60": 0.2054,
        "dte_60": 56,
        "expiry_60d": date(2026, 7, 28),
    }

    window = _volatility_cone_window(stats, latest, "rv60", 60)

    assert window["current"] == 0.2054
    assert window["current_iv"] == 0.2054
    assert window["current_rv"] == 0.3543
    assert window["current_source"] == "iv"
    assert window["dte"] == 56
    assert window["expiry"] == "2026-07-28"


def test_live_volatility_cone_overlay_matches_live_term_structure_dtes() -> None:
    result = {
        "current_reference_source": "symbol_daily_metrics",
        "x_axis_dtes": {"rv_60": 60},
        "cone": {
            "rv_60": {
                "current": 0.21,
                "current_iv": 0.21,
                "current_rv": 0.35,
                "current_source": "iv",
                "dte": 60,
                "expiry": "2026-07-30",
            }
        },
    }
    live = {
        "iv_term_structure_source": "nse:option-chain-v3",
        "iv_60": 0.2054,
        "dte_60": 56,
        "expiry_60d": "2026-07-28",
        "snapshot_time": "2026-06-02T06:45:00+00:00",
    }

    overlaid = _overlay_live_volatility_cone(result, live)

    assert overlaid["current_reference_source"] == "nse:option-chain-v3"
    assert overlaid["x_axis_dtes"]["rv_60"] == 56
    assert overlaid["cone"]["rv_60"]["current"] == 0.2054
    assert overlaid["cone"]["rv_60"]["current_iv"] == 0.2054
    assert overlaid["cone"]["rv_60"]["current_rv"] == 0.35
    assert overlaid["cone"]["rv_60"]["dte"] == 56
    assert overlaid["cone"]["rv_60"]["expiry"] == "2026-07-28"


def test_live_volatility_cone_overlay_keeps_eod_tenor_when_live_iv_missing() -> None:
    result = {
        "current_reference_source": "symbol_daily_metrics",
        "x_axis_dtes": {"rv_30": 30, "rv_90": 90},
        "cone": {
            "rv_30": {
                "current": 0.21,
                "current_iv": 0.21,
                "current_rv": 0.35,
                "current_source": "iv",
                "dte": 30,
                "expiry": "2026-06-30",
            },
            "rv_90": {
                "current": 0.27,
                "current_iv": 0.27,
                "current_rv": 0.31,
                "current_source": "iv",
                "dte": 90,
                "expiry": "2026-09-24",
            },
        },
    }
    live = {
        "iv_term_structure_source": "kite:quote:calculated-iv",
        "iv_30": 0.2054,
        "iv_90": None,
        "dte_30": 5,
        "dte_90": 61,
        "expiry_30d": "2026-06-30",
        "expiry_90d": "2026-08-25",
        "snapshot_time": "2026-06-25T12:23:19+05:30",
    }

    overlaid = _overlay_live_volatility_cone(result, live)

    assert overlaid["current_reference_source"] == "kite:quote:calculated-iv"
    assert overlaid["x_axis_dtes"] == {"rv_30": 5, "rv_90": 90}
    assert overlaid["cone"]["rv_30"]["current"] == 0.2054
    assert overlaid["cone"]["rv_30"]["dte"] == 5
    assert overlaid["cone"]["rv_90"]["current"] == 0.27
    assert overlaid["cone"]["rv_90"]["current_iv"] == 0.27
    assert overlaid["cone"]["rv_90"]["dte"] == 90
    assert overlaid["cone"]["rv_90"]["expiry"] == "2026-09-24"
    assert "current_iv_source" not in overlaid["cone"]["rv_90"]
