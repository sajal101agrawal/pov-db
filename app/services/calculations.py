from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import fmean
from typing import Iterable, Sequence

import numpy as np


TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365
MAX_ANALYTICS_IV = 2.0
MAX_ABS_ANALYTICS_SKEW = 0.75
MAX_ABS_RV_LOG_RETURN = math.log(3.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def years_to_expiry(trade_date: date, expiry_date: date) -> float:
    return max((expiry_date - trade_date).days, 0) / CALENDAR_DAYS_PER_YEAR


def black_scholes_price(
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> float:
    if time_to_expiry <= 0:
        return max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    if spot <= 0 or strike <= 0 or volatility <= 0:
        return 0.0

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility**2) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    df_q = math.exp(-dividend_yield * time_to_expiry)
    df_r = math.exp(-risk_free_rate * time_to_expiry)

    if option_type == "CE":
        return spot * df_q * _norm_cdf(d1) - strike * df_r * _norm_cdf(d2)
    return strike * df_r * _norm_cdf(-d2) - spot * df_q * _norm_cdf(-d1)


def implied_volatility_bisection(
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free_rate: float,
    option_type: str,
    dividend_yield: float = 0.0,
    lower_bound: float = 1e-8,
    upper_bound: float = 5.0,
    tolerance: float = 1e-8,
    max_iterations: int = 200,
) -> float | None:
    if market_price is None or market_price <= 0 or spot <= 0 or strike <= 0 or time_to_expiry <= 0:
        return None

    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    if market_price < intrinsic:
        return None

    lo = lower_bound
    hi = upper_bound
    price_hi = black_scholes_price(
        spot, strike, time_to_expiry, risk_free_rate, hi, option_type, dividend_yield
    )
    if price_hi < market_price:
        return None

    for _ in range(max_iterations):
        mid = (lo + hi) / 2.0
        price_mid = black_scholes_price(
            spot, strike, time_to_expiry, risk_free_rate, mid, option_type, dividend_yield
        )
        if abs(price_mid - market_price) <= tolerance:
            return mid
        if price_mid > market_price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def black_scholes_greeks(
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> Greeks:
    if time_to_expiry <= 0 or spot <= 0 or strike <= 0 or volatility <= 0:
        return Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility**2) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    df_q = math.exp(-dividend_yield * time_to_expiry)
    df_r = math.exp(-risk_free_rate * time_to_expiry)

    if option_type == "CE":
        delta = df_q * _norm_cdf(d1)
        theta = (
            -(spot * df_q * _norm_pdf(d1) * volatility) / (2.0 * sqrt_t)
            - risk_free_rate * strike * df_r * _norm_cdf(d2)
            + dividend_yield * spot * df_q * _norm_cdf(d1)
        ) / CALENDAR_DAYS_PER_YEAR
        rho = strike * time_to_expiry * df_r * _norm_cdf(d2) / 100.0
    else:
        delta = -df_q * _norm_cdf(-d1)
        theta = (
            -(spot * df_q * _norm_pdf(d1) * volatility) / (2.0 * sqrt_t)
            + risk_free_rate * strike * df_r * _norm_cdf(-d2)
            - dividend_yield * spot * df_q * _norm_cdf(-d1)
        ) / CALENDAR_DAYS_PER_YEAR
        rho = -strike * time_to_expiry * df_r * _norm_cdf(-d2) / 100.0

    gamma = df_q * _norm_pdf(d1) / (spot * volatility * sqrt_t)
    vega = spot * df_q * _norm_pdf(d1) * sqrt_t / 100.0
    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def atm_iv(call_iv: float | None, put_iv: float | None) -> float | None:
    values = [
        v
        for v in (call_iv, put_iv)
        if v is not None and math.isfinite(v) and 0 < v <= MAX_ANALYTICS_IV
    ]
    return fmean(values) if values else None


def constant_maturity_iv(
    near_iv: float,
    near_dte: int,
    far_iv: float,
    far_dte: int,
    target_dte: int,
) -> float | None:
    if min(near_iv, far_iv, near_dte, far_dte, target_dte) <= 0:
        return None
    if near_dte == target_dte:
        return near_iv
    if far_dte == target_dte:
        return far_iv
    if near_dte == far_dte:
        return near_iv

    t1 = near_dte / CALENDAR_DAYS_PER_YEAR
    t2 = far_dte / CALENDAR_DAYS_PER_YEAR
    target = target_dte / CALENDAR_DAYS_PER_YEAR
    var1 = near_iv**2 * t1
    var2 = far_iv**2 * t2
    w1 = (t2 - target) / (t2 - t1)
    w2 = (target - t1) / (t2 - t1)
    variance = (w1 * var1 + w2 * var2) / target
    return math.sqrt(max(variance, 0.0))


def realized_vol_close_to_close(closes: Sequence[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    arr = np.asarray(closes[-(window + 1) :], dtype=float)
    if np.any(arr <= 0):
        return None
    returns = np.diff(np.log(arr))
    if len(returns) < 2:
        return None
    return float(np.std(returns, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def yang_zhang_realized_vol(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> float | None:
    n = len(closes)
    if min(len(opens), len(highs), len(lows), n) != n or n < 3:
        return None
    o = np.log(np.asarray(opens, dtype=float))
    h = np.log(np.asarray(highs, dtype=float))
    l = np.log(np.asarray(lows, dtype=float))
    c = np.log(np.asarray(closes, dtype=float))
    if not all(np.all(np.isfinite(x)) for x in (o, h, l, c)):
        return None

    overnight = o[1:] - c[:-1]
    open_to_close = c[1:] - o[1:]
    close_to_close = c[1:] - c[:-1]
    if (
        np.max(np.abs(overnight)) > MAX_ABS_RV_LOG_RETURN
        or np.max(np.abs(open_to_close)) > MAX_ABS_RV_LOG_RETURN
        or np.max(np.abs(close_to_close)) > MAX_ABS_RV_LOG_RETURN
    ):
        return None
    rs = (h[1:] - c[1:]) * (h[1:] - o[1:]) + (l[1:] - c[1:]) * (l[1:] - o[1:])
    if len(overnight) < 2:
        return None

    overnight_var = float(np.var(overnight, ddof=1))
    open_close_var = float(np.var(open_to_close, ddof=1))
    rs_var = float(np.mean(rs))
    k = 0.34 / (1.34 + (len(overnight) + 1) / (len(overnight) - 1))
    variance = overnight_var + k * open_close_var + (1.0 - k) * rs_var
    if not math.isfinite(variance) or variance < 0:
        return None
    return math.sqrt(variance * TRADING_DAYS_PER_YEAR)


def volatility_risk_premium(iv_30: float | None, rv_30: float | None) -> float | None:
    if iv_30 is None or rv_30 is None:
        return None
    return iv_30 - rv_30


def forward_volatility(iv_30: float | None, iv_60: float | None, dte_30: int = 30, dte_60: int = 60) -> float | None:
    if iv_30 is None or iv_60 is None or iv_30 <= 0 or iv_60 <= 0 or dte_60 <= dte_30:
        return None
    variance = (iv_60**2 * dte_60 - iv_30**2 * dte_30) / (dte_60 - dte_30)
    if variance < 0:
        return None
    return math.sqrt(variance)


def forward_factor(iv_30: float | None, fwdv_3060: float | None) -> float | None:
    if iv_30 is None or fwdv_3060 is None or fwdv_3060 <= 0:
        return None
    return (iv_30 / fwdv_3060) - 1.0


def iv_slope(iv_30: float | None, iv_60: float | None, dte_30: int = 30, dte_60: int = 60) -> float | None:
    if iv_30 is None or iv_60 is None or dte_60 <= dte_30:
        return None
    return (iv_60 - iv_30) / (dte_60 - dte_30)


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def compute_skew(chain: Iterable[dict], target_delta: float) -> float | None:
    puts = [
        row
        for row in chain
        if row.get("option_type") == "PE"
        and row.get("iv") is not None
        and 0 < float(row["iv"]) <= MAX_ANALYTICS_IV
        and row.get("delta") is not None
    ]
    calls = [
        row
        for row in chain
        if row.get("option_type") == "CE"
        and row.get("iv") is not None
        and 0 < float(row["iv"]) <= MAX_ANALYTICS_IV
        and row.get("delta") is not None
    ]
    if not puts or not calls:
        return None
    put = min(puts, key=lambda row: abs(abs(float(row["delta"])) - target_delta))
    call = min(calls, key=lambda row: abs(float(row["delta"]) - target_delta))
    skew = float(put["iv"]) - float(call["iv"])
    return skew if abs(skew) <= MAX_ABS_ANALYTICS_SKEW else None


def smoothed_skew(*skews: float | None) -> float | None:
    values = [x for x in skews if x is not None and math.isfinite(x)]
    return fmean(values) if values else None


def percentile_rank(history: Sequence[float], current: float | None) -> float | None:
    values = [x for x in history if x is not None and math.isfinite(x)]
    if current is None or not values:
        return None
    less = sum(1 for x in values if x < current)
    equal = sum(1 for x in values if x == current)
    return 100.0 * (less + 0.5 * equal) / len(values)


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    arr = np.asarray(closes, dtype=float)
    if np.any(arr <= 0) or not np.all(np.isfinite(arr)):
        return None
    deltas = np.diff(arr)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + float(gain)) / period
        avg_loss = ((avg_loss * (period - 1)) + float(loss)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def straddle_pnl(call_entry: float, put_entry: float, call_exit: float, put_exit: float) -> float:
    return (call_entry + put_entry) - (call_exit + put_exit)
