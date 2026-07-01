from __future__ import annotations

from datetime import date, timedelta
import importlib.util
import math
from pathlib import Path

from app.services.forward_factors import compute_forward_factor_metrics


_BACKFILL_SPEC = importlib.util.spec_from_file_location(
    "backfill_forward_factors",
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_forward_factors.py",
)
assert _BACKFILL_SPEC is not None and _BACKFILL_SPEC.loader is not None
_BACKFILL_MODULE = importlib.util.module_from_spec(_BACKFILL_SPEC)
_BACKFILL_SPEC.loader.exec_module(_BACKFILL_MODULE)
audit_values = _BACKFILL_MODULE.audit_values
_metric_select_expression = _BACKFILL_MODULE._metric_select_expression


def test_forward_factors_use_separate_atm_call_and_put_iv_term_structures() -> None:
    trade_date = date(2026, 6, 1)
    chain = []
    for dte, call_iv, put_iv in ((5, 0.30, 0.20), (33, 0.27, 0.19), (61, 0.26, 0.18)):
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

    call_fwdv = math.sqrt((0.27**2 * 33 - 0.30**2 * 5) / (33 - 5))
    put_fwdv = math.sqrt((0.19**2 * 33 - 0.20**2 * 5) / (33 - 5))
    average_fwdv = math.sqrt((0.23**2 * 33 - 0.25**2 * 5) / (33 - 5))
    assert metrics["atm_strike"] == 100.0
    assert metrics["dte_30"] == 5
    assert metrics["dte_60"] == 33
    assert metrics["call_iv_30"] == 0.30
    assert metrics["put_iv_30"] == 0.20
    assert metrics["iv_30"] == 0.25
    assert math.isclose(metrics["call_fwdfct_3060"], 0.30 / call_fwdv - 1)
    assert math.isclose(metrics["put_fwdfct_3060"], 0.20 / put_fwdv - 1)
    assert math.isclose(metrics["fwdfct_3060"], 0.25 / average_fwdv - 1)


def test_missing_call_leg_does_not_borrow_put_iv_for_call_forward_factor() -> None:
    trade_date = date(2026, 6, 1)
    chain = [
        _option(trade_date + timedelta(days=30), 100.0, "CE", 0.30),
        _option(trade_date + timedelta(days=30), 100.0, "PE", 0.20),
        _option(trade_date + timedelta(days=60), 100.0, "PE", 0.19),
    ]

    metrics = compute_forward_factor_metrics(chain, trade_date, 100.0)

    assert metrics["call_iv_30"] == 0.30
    assert metrics["call_iv_60"] is None
    assert metrics["call_fwdfct_3060"] is None
    assert metrics["put_fwdfct_3060"] is not None


def test_forward_factors_use_same_near_atm_strike_for_far_expiry() -> None:
    trade_date = date(2026, 6, 1)
    chain = [
        _option(trade_date + timedelta(days=7), 100.0, "CE", 0.30),
        _option(trade_date + timedelta(days=7), 100.0, "PE", 0.20),
        _option(trade_date + timedelta(days=35), 105.0, "CE", 0.27),
        _option(trade_date + timedelta(days=35), 105.0, "PE", 0.19),
    ]

    metrics = compute_forward_factor_metrics(chain, trade_date, 101.0)

    assert metrics["atm_strike"] == 100.0
    assert metrics["call_iv_60"] is None
    assert metrics["put_iv_60"] is None
    assert metrics["call_fwdfct_3060"] is None
    assert metrics["put_fwdfct_3060"] is None


def test_forward_factors_skip_expiry_day_contracts() -> None:
    trade_date = date(2026, 6, 30)
    chain = []
    for dte, call_iv, put_iv in ((0, 0.80, 0.75), (28, 0.24, 0.22), (56, 0.21, 0.20)):
        expiry = trade_date + timedelta(days=dte)
        chain.extend(
            [
                _option(expiry, 100.0, "CE", call_iv),
                _option(expiry, 100.0, "PE", put_iv),
            ]
        )

    metrics = compute_forward_factor_metrics(chain, trade_date, 101.0)

    assert metrics["expiry_30d"] == date(2026, 7, 28)
    assert metrics["expiry_60d"] == date(2026, 8, 25)
    assert metrics["dte_30"] == 28
    assert metrics["dte_60"] == 56
    assert math.isclose(metrics["iv_30"], 0.23)
    assert math.isclose(metrics["iv_60"], 0.205)
    assert metrics["fwdv_3060"] is not None
    assert metrics["fwdfct_3060"] is not None


def test_forward_factor_backfill_handles_expiry_and_dte_fields() -> None:
    values = {
        "expiry_30d": date(2026, 7, 28),
        "dte_30": 27,
        "iv_30": 0.123456789,
    }

    assert _metric_select_expression("expiry_30d") == "sdm.expiry_30d"
    assert _metric_select_expression("dte_30") == "sdm.dte_30"
    assert _metric_select_expression("iv_30") == "sdm.iv_30::float"
    assert audit_values(values)["expiry_30d"] == "2026-07-28"
    assert audit_values(values)["dte_30"] == 27
    assert audit_values(values)["iv_30"] == 0.12345679


def _option(expiry: date, strike: float, option_type: str, iv: float) -> dict:
    return {
        "expiry_date": expiry,
        "strike_price": strike,
        "option_type": option_type,
        "iv": iv,
        "settle_price": 10.0,
        "close": 9.0,
    }
