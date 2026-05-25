from __future__ import annotations

from datetime import date

from app.sources.nse_option_chain import normalize_option_chain_summary
from app.sources.nse_option_chain import _format_expiry


def test_format_expiry_for_nse_v3() -> None:
    assert _format_expiry(date(2026, 5, 26)) == "26-May-2026"
    assert _format_expiry("2026-05-26") == "26-May-2026"
    assert _format_expiry("26-May-2026") == "26-May-2026"


def test_normalize_option_chain_summary_sums_ce_and_pe_volume() -> None:
    payload = {
        "records": {
            "timestamp": "25-May-2026 13:30:00",
            "underlyingValue": 1365,
            "data": [
                {
                    "strikePrice": 1360,
                    "CE": {"totalTradedVolume": 10, "impliedVolatility": 20},
                    "PE": {"totalTradedVolume": 20, "impliedVolatility": 30},
                },
                {
                    "strikePrice": 1370,
                    "CE": {"totalTradedVolume": 30, "impliedVolatility": 0},
                    "PE": {"totalTradedVolume": 40, "impliedVolatility": 25},
                },
            ],
        }
    }

    summary = normalize_option_chain_summary("RELIANCE", payload, "26-May-2026")

    assert summary is not None
    assert summary["live_option_volume"] == 100
    assert summary["live_option_volume_source"] == "nse:option-chain-v3"
    assert summary["live_atm_strike"] == 1360
    assert summary["live_atm_iv"] == 0.25
