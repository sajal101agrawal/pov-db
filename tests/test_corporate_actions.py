from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.corporate_actions import (
    STATUS_ADJUSTED,
    STATUS_PENDING,
    STATUS_SUSPICIOUS,
    adjust_ohlc_for_actions,
    calculate_price_series_metrics,
    derive_price_multiplier,
    parse_action_terms,
)
from app.sources.nse_corporate_actions import parse_nse_corporate_action
from scripts.backfill_corporate_action_metrics import _audit_values, _materially_changed


def test_bonus_and_split_terms_create_pre_ex_price_multipliers() -> None:
    bonus = parse_action_terms("Bonus 1:1", 10)
    split = parse_action_terms(
        "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 2/- Per Share",
        2,
    )

    assert bonus["action_type"] == "BONUS"
    assert bonus["price_multiplier"] == pytest.approx(0.5)
    assert bonus["adjustment_status"] == "VERIFIED"
    assert split["action_type"] == "SPLIT"
    assert split["price_multiplier"] == pytest.approx(0.2)


def test_dividend_and_rights_factors_are_derived_from_prior_close() -> None:
    dividend = parse_action_terms(
        "Interim Dividend - Rs 7 Per Share & Special Dividend Rs 3 Per Share", 2
    )
    dividend_multiplier, _ = derive_price_multiplier(dividend, 100.0)
    rights = parse_action_terms("Rights 1:9 @ Premium Rs 91/-", 10)
    rights_multiplier, _ = derive_price_multiplier(rights, 180.0)

    assert dividend["cash_amount"] == pytest.approx(10.0)
    assert dividend_multiplier == pytest.approx(0.9)
    assert rights["subscription_price"] == pytest.approx(101.0)
    assert rights_multiplier == pytest.approx((9 * 180 + 101) / 10 / 180)


def test_nse_action_parser_filters_non_equity_and_keeps_reliance_bonus() -> None:
    row = {
        "symbol": "RELIANCE",
        "series": "EQ",
        "subject": "Bonus 1:1",
        "exDate": "28-Oct-2024",
        "recDate": "28-Oct-2024",
        "faceVal": "10",
    }
    action = parse_nse_corporate_action(row)

    assert action is not None
    assert action["symbol"] == "RELIANCE"
    assert action["ex_date"] == date(2024, 10, 28)
    assert action["price_multiplier"] == pytest.approx(0.5)
    assert parse_nse_corporate_action({**row, "series": "GS"}) is None


def test_adjustment_scales_all_pre_ex_ohlc_fields_without_mutating_raw_rows() -> None:
    rows = [
        _row(date(2024, 10, 25), 100.0),
        _row(date(2024, 10, 28), 50.0),
    ]
    action = _action(date(2024, 10, 28), multiplier=0.5)

    result = adjust_ohlc_for_actions(rows, [action])

    assert result.status == STATUS_ADJUSTED
    assert rows[0]["open"] == 100.0
    assert result.rows[0]["open"] == pytest.approx(50.0)
    assert result.rows[0]["high"] == pytest.approx(50.5)
    assert result.rows[0]["low"] == pytest.approx(49.5)
    assert result.rows[0]["close"] == pytest.approx(50.0)
    assert result.rows[1]["close"] == pytest.approx(50.0)


def test_pending_factor_and_unexplained_gap_disable_adjusted_series() -> None:
    rows = [
        _row(date(2024, 10, 25), 100.0),
        _row(date(2024, 10, 28), 50.0),
    ]
    pending = _action(date(2024, 10, 28), multiplier=None)
    pending["adjustment_status"] = "PENDING_FACTOR"

    assert adjust_ohlc_for_actions(rows, [pending]).status == STATUS_PENDING
    assert adjust_ohlc_for_actions(rows, []).status == STATUS_SUSPICIOUS


def test_price_metrics_keep_raw_rv_but_use_adjusted_rv_for_signal() -> None:
    start = date(2024, 9, 20)
    ex_date = start + timedelta(days=20)
    rows = [
        _row(start + timedelta(days=index), 100.0 if index < 20 else 50.0) for index in range(40)
    ]
    metrics = calculate_price_series_metrics(rows, [_action(ex_date, 0.5)], rows[-1]["trade_date"])

    assert metrics["rv_data_status"] == STATUS_ADJUSTED
    assert metrics["rv_30"] is not None
    assert metrics["rv_30_raw"] is not None
    assert metrics["rv_30_raw"] > metrics["rv_30"]
    assert metrics["rv_calculation_version"] == 2


def test_backfill_audit_uses_database_precision_and_tracks_version_state() -> None:
    old = {
        "rv_30": 0.19892986,
        "rv_30_raw": 1.99776660,
        "daily_rsi": 54.5233,
        "rv_data_status": "CORPORATE_ACTION_ADJUSTED",
        "rv_calculation_version": 2,
        "vrp_signal_enabled": True,
    }
    recalculated = {
        **old,
        "rv_30": 0.1989298641,
        "rv_30_raw": 1.9977666041,
        "daily_rsi": 54.523306,
    }

    assert not _materially_changed(old, recalculated)
    assert _audit_values(recalculated)["daily_rsi"] == 54.5233
    assert _materially_changed(old, {**recalculated, "rv_calculation_version": 3})


def _row(trade_date: date, close: float) -> dict:
    return {
        "trade_date": trade_date,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
    }


def _action(ex_date: date, multiplier: float | None) -> dict:
    return {
        "id": 1,
        "ex_date": ex_date,
        "action_type": "BONUS",
        "description": "Bonus 1:1",
        "price_multiplier": multiplier,
        "adjustment_status": "VERIFIED" if multiplier is not None else "PENDING_FACTOR",
    }
