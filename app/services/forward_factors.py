from __future__ import annotations

from datetime import date
from typing import Any

from app.services.calculations import (
    MAX_ANALYTICS_IV,
    constant_maturity_iv,
    forward_factor,
    forward_volatility,
)


def compute_forward_factor_metrics(
    chain: list[dict[str, Any]],
    trade_date: date,
    spot_close: float,
) -> dict[str, float | date | int | None]:
    """Calculate average, call, and put ATM term structures and 30/60 factors.

    The ATM strike is selected independently for every expiry as the strike
    closest to the underlying close. Constant-maturity IV uses all available
    future expiries and the same variance interpolation as the legacy average
    calculation.
    """
    expiries = sorted(
        {
            row["expiry_date"]
            for row in chain
            if row.get("expiry_date") is not None and row["expiry_date"] >= trade_date
        }
    )
    expiry_buckets = monthly_expiry_buckets(expiries)
    selected_expiries = [
        expiry_buckets[index] if len(expiry_buckets) > index else None
        for index in range(3)
    ]

    terms: list[dict[str, Any]] = []
    by_expiry: dict[date, dict[str, Any]] = {}
    for expiry in expiries:
        rows = [row for row in chain if row.get("expiry_date") == expiry]
        strikes = {
            float(row["strike_price"])
            for row in rows
            if row.get("strike_price") is not None
        }
        if not strikes:
            continue
        strike = min(strikes, key=lambda value: (abs(value - spot_close), value))
        ce = next(
            (
                row
                for row in rows
                if float(row["strike_price"]) == strike and row.get("option_type") == "CE"
            ),
            None,
        )
        pe = next(
            (
                row
                for row in rows
                if float(row["strike_price"]) == strike and row.get("option_type") == "PE"
            ),
            None,
        )
        call_iv = analytics_iv(ce.get("iv") if ce else None)
        put_iv = analytics_iv(pe.get("iv") if pe else None)
        average_iv = _average_available(call_iv, put_iv)
        term = {
            "expiry_date": expiry,
            "dte": (expiry - trade_date).days,
            "strike": strike,
            "ce": ce,
            "pe": pe,
            "iv": average_iv,
            "call_iv": call_iv,
            "put_iv": put_iv,
        }
        terms.append(term)
        by_expiry[expiry] = term

    values: dict[str, float | date | int | None] = {}
    for tenor in (30, 60, 90):
        values[f"iv_{tenor}"] = constant_maturity_from_terms(terms, tenor, "iv")
        values[f"call_iv_{tenor}"] = constant_maturity_from_terms(
            terms, tenor, "call_iv"
        )
        values[f"put_iv_{tenor}"] = constant_maturity_from_terms(
            terms, tenor, "put_iv"
        )

    for index, tenor in enumerate((30, 60, 90)):
        expiry = selected_expiries[index]
        values[f"expiry_{tenor}d"] = expiry
        values[f"dte_{tenor}"] = (expiry - trade_date).days if expiry else None

    primary = by_expiry.get(selected_expiries[0]) if selected_expiries[0] else None
    ce = primary.get("ce") if primary else None
    pe = primary.get("pe") if primary else None
    values.update(
        {
            "atm_strike": primary.get("strike") if primary else None,
            "nearest_ce_iv": primary.get("call_iv") if primary else None,
            "nearest_pe_iv": primary.get("put_iv") if primary else None,
            "nearest_ce_ltp": _market_price(ce),
            "nearest_pe_ltp": _market_price(pe),
        }
    )

    average_fwdv = forward_volatility(values["iv_30"], values["iv_60"], 30, 60)
    call_fwdv = forward_volatility(
        values["call_iv_30"], values["call_iv_60"], 30, 60
    )
    put_fwdv = forward_volatility(
        values["put_iv_30"], values["put_iv_60"], 30, 60
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


def constant_maturity_from_terms(
    terms: list[dict[str, Any]], target_dte: int, value_key: str
) -> float | None:
    candidates = [
        (int(item["dte"]), float(item[value_key]))
        for item in terms
        if item.get(value_key) is not None
        and float(item[value_key]) > 0
        and int(item["dte"]) > 0
    ]
    if not candidates:
        return None
    exact = next((value for dte, value in candidates if dte == target_dte), None)
    if exact is not None:
        return exact
    below = [(dte, value) for dte, value in candidates if dte < target_dte]
    above = [(dte, value) for dte, value in candidates if dte > target_dte]
    if below and above:
        near = max(below, key=lambda item: item[0])
        far = min(above, key=lambda item: item[0])
        return constant_maturity_iv(near[1], near[0], far[1], far[0], target_dte)
    return min(candidates, key=lambda item: abs(item[0] - target_dte))[1]


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
