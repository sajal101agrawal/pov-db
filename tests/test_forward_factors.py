from __future__ import annotations

from datetime import date, timedelta
import math

from app.services.forward_factors import compute_forward_factor_metrics


def test_forward_factors_use_separate_atm_call_and_put_iv_term_structures() -> None:
    trade_date = date(2026, 6, 1)
    chain = []
    for dte, call_iv, put_iv in ((30, 0.30, 0.20), (60, 0.27, 0.19), (90, 0.26, 0.18)):
        expiry = trade_date + timedelta(days=dte)
        chain.extend(
            [
                _option(expiry, 100.0, "CE", call_iv),
                _option(expiry, 100.0, "PE", put_iv),
                _option(expiry, 110.0, "CE", 0.99),
                _option(expiry, 110.0, "PE", 0.99),
            ]
        )

    metrics = compute_forward_factor_metrics(chain, trade_date, 102.0)

    assert metrics["atm_strike"] == 100.0
    assert metrics["call_iv_30"] == 0.30
    assert metrics["put_iv_30"] == 0.20
    assert metrics["iv_30"] == 0.25
    assert math.isclose(metrics["call_fwdfct_3060"], 0.30 / 0.27 - 1)
    assert math.isclose(metrics["put_fwdfct_3060"], 0.20 / 0.19 - 1)
    assert math.isclose(metrics["fwdfct_3060"], 0.25 / 0.23 - 1)


def test_missing_call_leg_does_not_borrow_put_iv_for_call_forward_factor() -> None:
    trade_date = date(2026, 6, 1)
    chain = [
        _option(trade_date + timedelta(days=30), 100.0, "CE", 0.30),
        _option(trade_date + timedelta(days=30), 100.0, "PE", 0.20),
        _option(trade_date + timedelta(days=60), 100.0, "PE", 0.19),
    ]

    metrics = compute_forward_factor_metrics(chain, trade_date, 100.0)

    assert metrics["call_iv_30"] == 0.30
    assert metrics["call_iv_60"] == 0.30
    assert metrics["call_fwdfct_3060"] == 0.0
    assert metrics["put_fwdfct_3060"] is not None


def _option(expiry: date, strike: float, option_type: str, iv: float) -> dict:
    return {
        "expiry_date": expiry,
        "strike_price": strike,
        "option_type": option_type,
        "iv": iv,
        "settle_price": 10.0,
        "close": 9.0,
    }
