from __future__ import annotations

from datetime import date
from typing import Any

from app.services.calculations import (
    MAX_ANALYTICS_IV,
    forward_factor,
    forward_volatility,
)


def compute_forward_factor_metrics(
    chain: list[dict[str, Any]],
    trade_date: date,
    spot_close: float,
) -> dict[str, float | date | int | None]:
    """Calculate average, call, and put ATM term structures and forward factors.

    The ATM strike is selected from the first available expiry bucket using the
    same-day spot close. Later expiry buckets use that same strike so the
    historical calculation mirrors the calendar-spread trade.
    """
    expiries = sorted(
        {
            row["expiry_date"]
            for row in chain
            if row.get("expiry_date") is not None and row["expiry_date"] > trade_date
        }
    )
    expiry_buckets = monthly_expiry_buckets(expiries)
    selected_expiries = [
        expiry_buckets[index] if len(expiry_buckets) > index else None
        for index in range(3)
    ]

    near_expiry = selected_expiries[0]
    near_rows = _rows_for_expiry(chain, near_expiry) if near_expiry else []
    near_strikes = _available_strikes(near_rows)
    atm_strike = (
        min(near_strikes, key=lambda value: (abs(value - spot_close), value))
        if near_strikes
        else None
    )

    terms: list[dict[str, Any] | None] = [
        _term_for_expiry(chain, expiry, trade_date, atm_strike)
        if expiry is not None and atm_strike is not None
        else None
        for expiry in selected_expiries
    ]

    values: dict[str, float | date | int | None] = {}
    for index, tenor in enumerate((30, 60, 90)):
        values[f"iv_{tenor}"] = _term_value(terms, index, "iv")
        values[f"call_iv_{tenor}"] = _term_value(terms, index, "call_iv")
        values[f"put_iv_{tenor}"] = _term_value(terms, index, "put_iv")

    for index, tenor in enumerate((30, 60, 90)):
        expiry = selected_expiries[index]
        values[f"expiry_{tenor}d"] = expiry
        values[f"dte_{tenor}"] = (expiry - trade_date).days if expiry else None

    primary = terms[0]
    ce = primary.get("ce") if primary else None
    pe = primary.get("pe") if primary else None
    values.update(
        {
            "atm_strike": atm_strike,
            "nearest_ce_iv": primary.get("call_iv") if primary else None,
            "nearest_pe_iv": primary.get("put_iv") if primary else None,
            "nearest_ce_ltp": _market_price(ce),
            "nearest_pe_ltp": _market_price(pe),
        }
    )

    dte_30 = values["dte_30"]
    dte_60 = values["dte_60"]
    average_fwdv = forward_volatility(
        values["iv_30"], values["iv_60"], dte_30 or 30, dte_60 or 60
    )
    call_fwdv = forward_volatility(
        values["call_iv_30"], values["call_iv_60"], dte_30 or 30, dte_60 or 60
    )
    put_fwdv = forward_volatility(
        values["put_iv_30"], values["put_iv_60"], dte_30 or 30, dte_60 or 60
    )
    values.update(
        {
            "fwdv_3060": average_fwdv,
            "fwdfct_3060": forward_factor(values["iv_30"], average_fwdv),
            "call_fwdfct_3060": forward_factor(values["call_iv_30"], call_fwdv),
            "put_fwdfct_3060": forward_factor(values["put_iv_30"], put_fwdv),
        }
    )
    return values


def _term_value(terms: list[dict[str, Any] | None], index: int, key: str) -> float | None:
    if index >= len(terms) or terms[index] is None:
        return None
    return terms[index].get(key)


def _term_for_expiry(
    chain: list[dict[str, Any]],
    expiry: date,
    trade_date: date,
    strike: float,
) -> dict[str, Any]:
    rows = _rows_for_expiry(chain, expiry)
    ce = _leg_at_strike(rows, strike, "CE")
    pe = _leg_at_strike(rows, strike, "PE")
    call_iv = analytics_iv(ce.get("iv") if ce else None)
    put_iv = analytics_iv(pe.get("iv") if pe else None)
    average_iv = _average_available(call_iv, put_iv)
    return {
        "expiry_date": expiry,
        "dte": (expiry - trade_date).days,
        "strike": strike,
        "ce": ce,
        "pe": pe,
        "iv": average_iv,
        "call_iv": call_iv,
        "put_iv": put_iv,
    }


def _rows_for_expiry(
    chain: list[dict[str, Any]],
    expiry: date | None,
) -> list[dict[str, Any]]:
    return [row for row in chain if row.get("expiry_date") == expiry]


def _available_strikes(rows: list[dict[str, Any]]) -> set[float]:
    return {
        float(row["strike_price"])
        for row in rows
        if row.get("strike_price") is not None
    }


def _leg_at_strike(
    rows: list[dict[str, Any]],
    strike: float,
    option_type: str,
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in rows
            if float(row["strike_price"]) == strike
            and row.get("option_type") == option_type
        ),
        None,
    )


def monthly_expiry_buckets(expiries: list[date]) -> list[date]:
    monthly: dict[tuple[int, int], date] = {}
    for expiry in sorted(expiries):
        monthly[(expiry.year, expiry.month)] = expiry
    selected = sorted(monthly.values())
    return selected[:3] if len(selected) >= 3 else sorted(expiries)[:3]


def analytics_iv(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if 0 < number <= MAX_ANALYTICS_IV else None


def _average_available(call_iv: float | None, put_iv: float | None) -> float | None:
    values = [value for value in (call_iv, put_iv) if value is not None]
    return sum(values) / len(values) if values else None


def _market_price(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    value = row.get("settle_price") or row.get("close")
    return float(value) if value is not None else None
