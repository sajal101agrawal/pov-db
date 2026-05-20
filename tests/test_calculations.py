from __future__ import annotations

import math
from datetime import date

from app.etl.pipeline import _monthly_expiry_buckets

from app.services.calculations import (
    atm_iv,
    black_scholes_greeks,
    black_scholes_price,
    compute_skew,
    forward_factor,
    forward_volatility,
    implied_volatility_bisection,
    iv_slope,
    rsi,
    smoothed_skew,
    straddle_pnl,
    volatility_risk_premium,
    yang_zhang_realized_vol,
)


def test_black_scholes_iv_round_trip() -> None:
    price = black_scholes_price(100.0, 100.0, 30 / 365, 0.06, 0.25, "CE")
    iv = implied_volatility_bisection(price, 100.0, 100.0, 30 / 365, 0.06, "CE")
    assert iv is not None
    assert math.isclose(iv, 0.25, rel_tol=1e-6)


def test_greeks_signs() -> None:
    call = black_scholes_greeks(100.0, 100.0, 30 / 365, 0.06, 0.25, "CE")
    put = black_scholes_greeks(100.0, 100.0, 30 / 365, 0.06, 0.25, "PE")
    assert call.delta > 0
    assert put.delta < 0
    assert call.gamma > 0
    assert put.gamma > 0
    assert call.vega > 0


def test_pdf_forward_factor_formula() -> None:
    fwdv = forward_volatility(0.20, 0.30, 30, 60)
    assert fwdv is not None
    assert math.isclose(fwdv, math.sqrt((0.30**2 * 60 - 0.20**2 * 30) / 30))
    assert math.isclose(forward_factor(0.20, fwdv), fwdv / 0.20)


def test_iv_slope_uses_daily_dte_gap() -> None:
    assert math.isclose(iv_slope(0.20, 0.26, 30, 60), (0.26 - 0.20) / 30)


def test_vrp_and_atm_iv_are_decimal_native() -> None:
    assert atm_iv(0.20, 0.30) == 0.25
    assert volatility_risk_premium(0.25, 0.18) == 0.07


def test_monthly_expiry_buckets_do_not_choose_closest_dte() -> None:
    expiries = [
        date(2021, 6, 24),
        date(2021, 7, 29),
        date(2021, 8, 26),
    ]
    selected = _monthly_expiry_buckets(expiries)
    assert selected == expiries
    assert [(expiry - date(2021, 6, 14)).days for expiry in selected] == [10, 45, 73]


def test_monthly_expiry_buckets_collapse_index_weeklies() -> None:
    selected = _monthly_expiry_buckets(
        [
            date(2026, 5, 21),
            date(2026, 5, 28),
            date(2026, 6, 4),
            date(2026, 6, 25),
            date(2026, 7, 30),
        ]
    )
    assert selected == [date(2026, 5, 28), date(2026, 6, 25), date(2026, 7, 30)]


def test_skew_and_smoothed_skew() -> None:
    chain = [
        {"option_type": "CE", "delta": 0.18, "iv": 0.20},
        {"option_type": "CE", "delta": 0.25, "iv": 0.22},
        {"option_type": "PE", "delta": -0.19, "iv": 0.27},
        {"option_type": "PE", "delta": -0.25, "iv": 0.30},
    ]
    assert math.isclose(compute_skew(chain, 0.20), 0.27 - 0.20)
    assert math.isclose(smoothed_skew(0.1, None, 0.2), 0.15)


def test_yang_zhang_returns_annualized_positive_vol() -> None:
    opens = [100, 101, 102, 101, 103, 104, 103, 105, 106, 107, 106]
    highs = [101, 102, 103, 102, 104, 105, 104, 106, 107, 108, 107]
    lows = [99, 100, 101, 100, 102, 103, 102, 104, 105, 106, 105]
    closes = [100.5, 101.5, 101.2, 102.5, 103.5, 103.2, 104.8, 105.5, 106.5, 106.2, 107.5]
    vol = yang_zhang_realized_vol(opens, highs, lows, closes)
    assert vol is not None
    assert vol > 0


def test_rsi_and_straddle_pnl() -> None:
    assert rsi(list(range(1, 17)), 14) == 100.0
    assert straddle_pnl(10, 12, 6, 9) == 7
