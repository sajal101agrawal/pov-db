from __future__ import annotations

import math
from datetime import date

from app.services.live import _live_forward_metrics
from app.sources.nse_option_chain import normalize_option_chain_payload, normalize_option_chain_summary
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
    assert summary["live_option_expiry_date"] == date(2026, 5, 26)
    assert summary["live_atm_strike"] == 1360
    assert summary["live_atm_iv"] == 0.25


def test_normalize_option_chain_payload_matches_live_chain_shape() -> None:
    payload = {
        "records": {
            "timestamp": "25-May-2026 13:30:00",
            "underlyingValue": 1365,
            "data": [
                {
                    "strikePrice": 1360,
                    "CE": {
                        "identifier": "CE-ID",
                        "lastPrice": 12.5,
                        "bidprice": 12.1,
                        "askPrice": 12.8,
                        "totalTradedVolume": 10,
                        "openInterest": 100,
                        "impliedVolatility": 20,
                    },
                    "PE": {
                        "identifier": "PE-ID",
                        "lastPrice": 10.5,
                        "bidprice": 10.1,
                        "askPrice": 10.8,
                        "totalTradedVolume": 20,
                        "openInterest": 200,
                        "impliedVolatility": 30,
                    },
                }
            ],
        }
    }

    chain = normalize_option_chain_payload("RELIANCE", payload, "26-May-2026")

    assert chain is not None
    assert chain["provider"] == "nse"
    assert chain["expiry"] == "2026-05-26"
    assert chain["underlying_last_price"] == 1365
    assert chain["strike_count"] == 1
    assert chain["strikes"][0]["ce"]["last_price"] == 12.5
    assert chain["strikes"][0]["ce"]["implied_volatility"] == 0.20
    assert chain["strikes"][0]["pe"]["volume"] == 20


def test_live_forward_metrics_use_live_term_structure() -> None:
    summary = {
        "live_iv_terms": [
            {"expiry_date": date(2026, 6, 27), "atm_iv": 0.20},
            {"expiry_date": date(2026, 7, 27), "atm_iv": 0.25},
            {"expiry_date": date(2026, 8, 26), "atm_iv": 0.30},
        ]
    }

    metrics = _live_forward_metrics(summary, date(2026, 5, 28))

    expected_fwdv = math.sqrt((0.25**2 * 60 - 0.20**2 * 30) / 30)
    assert metrics["iv_30"] == 0.20
    assert metrics["iv_60"] == 0.25
    assert metrics["iv_90"] == 0.30
    assert math.isclose(metrics["fwdv_3060"], expected_fwdv)
    assert math.isclose(metrics["fwdfct_3060"], (0.20 / expected_fwdv) - 1.0)
    assert math.isclose(metrics["iv_slope_3060"], (0.25 - 0.20) / 30)
    assert metrics["iv_term_structure_source"] == "nse:option-chain-v3"


def test_live_forward_metrics_do_not_invent_missing_far_tenor() -> None:
    summary = {
        "live_iv_terms": [
            {"expiry_date": date(2026, 6, 30), "atm_iv": 0.20},
        ]
    }

    metrics = _live_forward_metrics(summary, date(2026, 5, 28))

    assert metrics["iv_30"] == 0.20
    assert metrics["iv_60"] is None
    assert metrics["iv_90"] is None
    assert metrics["fwdv_3060"] is None
    assert metrics["fwdfct_3060"] is None
