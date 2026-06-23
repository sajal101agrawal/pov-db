from __future__ import annotations

import asyncio
from datetime import date, timedelta
import json
from typing import Any

from app.core.config import Settings
from app.db.repository import MarketRepository
from app.services.calculations import (
    MAX_ANALYTICS_IV,
    atm_iv,
    black_scholes_greeks,
    constant_maturity_iv,
    compute_skew,
    forward_factor,
    forward_volatility,
    implied_volatility_bisection,
    iv_slope,
    ratio,
    smoothed_skew,
    straddle_pnl,
    volatility_risk_premium,
    years_to_expiry,
)
from app.services.corporate_actions import (
    USABLE_RV_STATUSES,
    calculate_price_series_metrics,
)
from app.sources.bhavcopy import BhavcopySource
from app.sources.nse_corporate_actions import NSECorporateActionsClient
from app.sources.rates import IndiaRiskFreeRateClient


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        repository: MarketRepository,
        bhavcopy_source: BhavcopySource,
        rates: IndiaRiskFreeRateClient,
        corporate_actions_source: NSECorporateActionsClient | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.bhavcopy_source = bhavcopy_source
        self.rates = rates
        self.corporate_actions_source = corporate_actions_source

    async def run_for_date(
        self,
        trade_date: date,
        symbols: list[str] | None = None,
        finalize: bool = True,
        sync_corporate_actions: bool = True,
    ) -> dict[str, Any]:
        action_sync_start = trade_date - timedelta(days=180)
        action_sync_end = trade_date + timedelta(days=45)
        action_task = (
            self.corporate_actions_source.fetch_actions(
                action_sync_start, action_sync_end, symbols
            )
            if sync_corporate_actions and self.corporate_actions_source is not None
            else asyncio.sleep(0, result=[])
        )
        fo_rows, cm_rows, corporate_actions = await asyncio.gather(
            self.bhavcopy_source.fetch_fo(trade_date),
            self.bhavcopy_source.fetch_cm(trade_date),
            action_task,
        )
        if symbols:
            allowed = {s.upper() for s in symbols}
            fo_rows = [row for row in fo_rows if row.symbol in allowed]
            cm_rows = [row for row in cm_rows if row.symbol in allowed]

        option_count = await self.repository.upsert_option_rows(fo_rows)
        equity_count = await self.repository.upsert_equity_rows(cm_rows)
        await self.repository.upsert_discovered_symbols(_discovered_symbols(fo_rows, cm_rows))

        symbols_for_metrics = sorted({row.symbol for row in fo_rows})
        if corporate_actions:
            allowed_actions = set(symbols_for_metrics)
            corporate_actions = [
                action for action in corporate_actions if action["symbol"] in allowed_actions
            ]
            await self.repository.upsert_corporate_actions(corporate_actions)
        action_resolution = await self.repository.resolve_corporate_action_factors(
            start=action_sync_start if sync_corporate_actions else trade_date,
            end=trade_date,
            symbols=symbols_for_metrics,
        )

        rates = await self.rates.fetch_91d_rate(trade_date - timedelta(days=10), trade_date + timedelta(days=1))
        await self.repository.upsert_interest_rates(rates)
        await self.repository.refresh_expiry_calendar(trade_date)

        if self.settings.pipeline_symbol_limit:
            symbols_for_metrics = symbols_for_metrics[: self.settings.pipeline_symbol_limit]

        concurrency = max(1, int(self.settings.pipeline_compute_concurrency))
        if concurrency == 1:
            for symbol in symbols_for_metrics:
                await self.compute_symbol_day(symbol, trade_date)
        else:
            semaphore = asyncio.Semaphore(concurrency)

            async def compute_with_limit(symbol: str) -> None:
                async with semaphore:
                    await self.compute_symbol_day(symbol, trade_date)

            await asyncio.gather(*(compute_with_limit(symbol) for symbol in symbols_for_metrics))

        if finalize:
            await self.repository.refresh_percentiles(trade_date)
            await self.repository.refresh_aggregates()
        return {
            "trade_date": trade_date.isoformat(),
            "options_rows": option_count,
            "equity_rows": equity_count,
            "symbols": len(symbols_for_metrics),
            "corporate_actions": len(corporate_actions),
            "corporate_action_factors": action_resolution,
            "finalized": finalize,
        }

    async def compute_symbol_day(self, symbol: str, trade_date: date) -> None:
        rate = await self.repository.risk_free_rate(trade_date, self.settings.default_risk_free_rate)
        ohlc = await self.repository.equity_ohlc_window(symbol, trade_date, limit=100)
        if not ohlc:
            return
        spot_row = ohlc[-1]
        spot_close = spot_row.get("close")
        spot_open = spot_row.get("open") or spot_close
        if not spot_close:
            return
        corporate_actions = await self.repository.corporate_actions_window(
            symbol, ohlc[0]["trade_date"], trade_date
        )

        chain = await self.repository.option_chain(symbol, trade_date)
        if not chain:
            return
        atm_strike = min(
            {float(r["strike_price"]) for r in chain},
            key=lambda strike: (abs(strike - spot_close), strike),
        )

        derived = []
        for row in chain:
            option_type = row["option_type"]
            t = years_to_expiry(trade_date, row["expiry_date"])
            market_price = row.get("settle_price") or row.get("close")
            iv = implied_volatility_bisection(
                market_price or 0.0,
                spot_close,
                float(row["strike_price"]),
                t,
                rate,
                option_type,
            )
            greeks = (
                black_scholes_greeks(
                    spot_close,
                    float(row["strike_price"]),
                    t,
                    rate,
                    iv,
                    option_type,
                )
                if iv is not None
                else None
            )
            item = dict(row)
            item.update(
                {
                    "iv": iv,
                    "delta": greeks.delta if greeks else None,
                    "gamma": greeks.gamma if greeks else None,
                    "theta": greeks.theta if greeks else None,
                    "vega": greeks.vega if greeks else None,
                    "rho": greeks.rho if greeks else None,
                    "is_atm": float(row["strike_price"]) == atm_strike,
                }
            )
            derived.append(item)
        await self.repository.update_contract_derived(derived)

        fresh_chain = await self.repository.option_chain(symbol, trade_date)
        metrics = self._compute_metric(
            symbol,
            trade_date,
            spot_open,
            spot_close,
            ohlc,
            corporate_actions,
            fresh_chain,
            atm_strike,
        )
        lagged_iv30 = await self.repository.lagged_iv30(symbol, trade_date, 20)
        metrics["vrp"] = (
            volatility_risk_premium(lagged_iv30, metrics.get("rv_30"))
            if metrics.get("rv_data_status") in USABLE_RV_STATUSES
            else None
        )
        metrics["vrp_signal_enabled"] = metrics["vrp"] is not None
        metrics["rv_adjustment_details"] = json.dumps(
            metrics["rv_adjustment_details"], default=str
        )
        await self.repository.upsert_daily_metric(metrics)
        await self._compute_straddle(symbol, trade_date, spot_open, spot_close, metrics)

    def _compute_metric(
        self,
        symbol: str,
        trade_date: date,
        spot_open: float,
        spot_close: float,
        ohlc: list[dict[str, Any]],
        corporate_actions: list[dict[str, Any]],
        chain: list[dict[str, Any]],
        atm_strike: float,
    ) -> dict[str, Any]:
        expiries = sorted({row["expiry_date"] for row in chain if row["expiry_date"] >= trade_date})
        expiry_buckets = _monthly_expiry_buckets(expiries)
        expiry_30 = expiry_buckets[0] if len(expiry_buckets) >= 1 else None
        expiry_60 = expiry_buckets[1] if len(expiry_buckets) >= 2 else None
        expiry_90 = expiry_buckets[2] if len(expiry_buckets) >= 3 else None

        def atm_for_expiry(expiry):
            if not expiry:
                return None, None, None, None
            rows = [r for r in chain if r["expiry_date"] == expiry]
            if not rows:
                return None, None, None, None
            strike = min(
                {float(r["strike_price"]) for r in rows},
                key=lambda value: (abs(value - spot_close), value),
            )
            ce = next((r for r in rows if float(r["strike_price"]) == strike and r["option_type"] == "CE"), None)
            pe = next((r for r in rows if float(r["strike_price"]) == strike and r["option_type"] == "PE"), None)
            return strike, ce, pe, atm_iv(ce.get("iv") if ce else None, pe.get("iv") if pe else None)

        strike30, ce30, pe30, iv30 = atm_for_expiry(expiry_30)
        _strike60, _ce60, _pe60, iv60 = atm_for_expiry(expiry_60)
        _strike90, _ce90, _pe90, iv90 = atm_for_expiry(expiry_90)
        iv30 = _constant_maturity_atm_iv(expiries, trade_date, atm_for_expiry, 30)
        iv60 = _constant_maturity_atm_iv(expiries, trade_date, atm_for_expiry, 60)
        iv90 = _constant_maturity_atm_iv(expiries, trade_date, atm_for_expiry, 90)

        dte30 = (expiry_30 - trade_date).days if expiry_30 else None
        dte60 = (expiry_60 - trade_date).days if expiry_60 else None
        dte90 = (expiry_90 - trade_date).days if expiry_90 else None
        price_metrics = calculate_price_series_metrics(ohlc, corporate_actions, trade_date)
        rv10 = price_metrics["rv_10"]
        rv20 = price_metrics["rv_20"]
        rv30 = price_metrics["rv_30"]
        rv60 = price_metrics["rv_60"]
        rv90 = price_metrics["rv_90"]
        fwdv = forward_volatility(iv30, iv60, 30, 60)
        skew_expiry = _expiry_closest_to_target(expiries, trade_date, 30)
        skew_chain = [row for row in chain if row["expiry_date"] == skew_expiry]
        skew20 = compute_skew(skew_chain, 0.20)
        skew25 = compute_skew(skew_chain, 0.25)
        skew30 = compute_skew(skew_chain, 0.30)

        return {
            "symbol": symbol,
            "trade_date": trade_date,
            "iv_30": iv30,
            "iv_60": iv60,
            "iv_90": iv90,
            "expiry_30d": expiry_30,
            "expiry_60d": expiry_60,
            "expiry_90d": expiry_90,
            "dte_30": dte30,
            "dte_60": dte60,
            "dte_90": dte90,
            "atm_strike": strike30 or atm_strike,
            "nearest_ce_iv": _analytics_iv(ce30.get("iv") if ce30 else None),
            "nearest_pe_iv": _analytics_iv(pe30.get("iv") if pe30 else None),
            "nearest_ce_ltp": (ce30.get("settle_price") or ce30.get("close")) if ce30 else None,
            "nearest_pe_ltp": (pe30.get("settle_price") or pe30.get("close")) if pe30 else None,
            "rv_10": rv10,
            "rv_20": rv20,
            "rv_30": rv30,
            "rv_60": rv60,
            "rv_90": rv90,
            "rv_10_raw": price_metrics["rv_10_raw"],
            "rv_20_raw": price_metrics["rv_20_raw"],
            "rv_30_raw": price_metrics["rv_30_raw"],
            "rv_60_raw": price_metrics["rv_60_raw"],
            "rv_90_raw": price_metrics["rv_90_raw"],
            "rv_data_status": price_metrics["rv_data_status"],
            "rv_adjustment_details": price_metrics["rv_adjustment_details"],
            "rv_calculation_version": price_metrics["rv_calculation_version"],
            "vrp": None,
            "vrp_signal_enabled": False,
            "fwdv_3060": fwdv,
            "fwdfct_3060": forward_factor(iv30, fwdv),
            "fev_30": fwdv,
            "iv_slope_3060": iv_slope(iv30, iv60, 30, 60),
            "skew_20": skew20,
            "skew_25": skew25,
            "skew_30": skew30,
            "smoothed_skew": smoothed_skew(skew20, skew25, skew30),
            "iv30_rv30_ratio": ratio(iv30, rv30),
            "iv30_fev30_ratio": ratio(iv30, fwdv),
            "avg_option_volume": _total_option_volume(chain),
            "daily_rsi": price_metrics["daily_rsi"],
            "weekly_rsi": price_metrics["weekly_rsi"],
        }

    async def _compute_straddle(
        self,
        symbol: str,
        trade_date: date,
        spot_open: float,
        spot_close: float,
        metrics: dict[str, Any],
    ) -> None:
        expiries = [
            row["expiry_date"]
            for row in await self.repository.pool.fetch(
                """
                SELECT DISTINCT expiry_date
                FROM options_historical
                WHERE symbol = $1 AND trade_date = $2 AND expiry_date >= $2
                ORDER BY expiry_date
                """,
                symbol,
                trade_date,
            )
        ]
        expiry = metrics.get("expiry_30d")
        if expiry not in expiries:
            expiry_buckets = _monthly_expiry_buckets(expiries)
            expiry = expiry_buckets[0] if expiry_buckets else None
        if not expiry:
            await self.repository.upsert_straddle_pnl({"symbol": symbol, "trade_date": trade_date, "skip_reason": "NO_EXPIRY"})
            return
        chain = await self.repository.option_chain(symbol, trade_date, expiry)
        if not chain:
            await self.repository.upsert_straddle_pnl({"symbol": symbol, "trade_date": trade_date, "skip_reason": "NO_DATA"})
            return
        strike = min(
            {float(r["strike_price"]) for r in chain},
            key=lambda value: (abs(value - spot_open), value),
        )
        ce = next((r for r in chain if float(r["strike_price"]) == strike and r["option_type"] == "CE"), None)
        pe = next((r for r in chain if float(r["strike_price"]) == strike and r["option_type"] == "PE"), None)
        if not ce or not pe or ce.get("open") is None or pe.get("open") is None or ce.get("close") is None or pe.get("close") is None:
            await self.repository.upsert_straddle_pnl({"symbol": symbol, "trade_date": trade_date, "skip_reason": "MISSING_LEGS"})
            return
        pnl = straddle_pnl(ce["open"], pe["open"], ce["close"], pe["close"])
        has_result_event = await self.repository.has_event(symbol, trade_date, "RESULT")
        await self.repository.upsert_straddle_pnl(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "expiry_date": expiry,
                "atm_strike": strike,
                "underlying_open": spot_open,
                "underlying_close": spot_close,
                "underlying_move_pct": ((spot_close - spot_open) / spot_open * 100.0) if spot_open else None,
                "call_entry": ce["open"],
                "put_entry": pe["open"],
                "total_entry": ce["open"] + pe["open"],
                "call_exit": ce["close"],
                "put_exit": pe["close"],
                "total_exit": ce["close"] + pe["close"],
                "pnl": pnl,
                "is_winner": pnl > 0,
                "has_result_event": has_result_event,
                "iv_on_entry": metrics.get("iv_30"),
                "skip_reason": None,
            }
        )


def _monthly_expiry_buckets(expiries: list[date]) -> list[date]:
    """Return the first three monthly expiry buckets.

    NSE index options can have weekly expiries. The prior processing project
    used the latest expiry in each calendar month as the monthly contract.
    Stock options usually only have monthly expiries, so this is simply the
    sorted first/second/third available expiries for those symbols.
    """
    monthly: dict[tuple[int, int], date] = {}
    for expiry in sorted(expiries):
        monthly[(expiry.year, expiry.month)] = expiry
    selected = sorted(monthly.values())
    return selected[:3] if len(selected) >= 3 else sorted(expiries)[:3]


def _constant_maturity_atm_iv(expiries, trade_date: date, atm_for_expiry, target_dte: int) -> float | None:
    candidates = []
    for expiry in expiries:
        dte = (expiry - trade_date).days
        _strike, _ce, _pe, iv = atm_for_expiry(expiry)
        if iv is not None and iv > 0:
            candidates.append((expiry, dte, iv))
    if not candidates:
        return None
    exact = next((iv for _expiry, dte, iv in candidates if dte == target_dte), None)
    if exact is not None:
        return exact
    below = [item for item in candidates if item[1] < target_dte]
    above = [item for item in candidates if item[1] > target_dte]
    if below and above:
        near = max(below, key=lambda item: item[1])
        far = min(above, key=lambda item: item[1])
        return constant_maturity_iv(near[2], near[1], far[2], far[1], target_dte)
    return min(candidates, key=lambda item: abs(item[1] - target_dte))[2]


def _expiry_closest_to_target(expiries: list[date], trade_date: date, target_dte: int) -> date | None:
    candidates = [expiry for expiry in expiries if expiry >= trade_date]
    if not candidates:
        return None
    return min(candidates, key=lambda expiry: abs((expiry - trade_date).days - target_dte))


def _total_option_volume(chain: list[dict[str, Any]]) -> float | None:
    volumes = [
        float(row["num_contracts"])
        for row in chain
        if row.get("option_type") in {"CE", "PE"} and row.get("num_contracts") is not None
    ]
    return sum(volumes) if volumes else None


def _analytics_iv(value: float | None) -> float | None:
    return value if value is not None and 0 < float(value) <= MAX_ANALYTICS_IV else None


def _discovered_symbols(fo_rows, cm_rows) -> list[dict[str, Any]]:
    symbols: dict[str, str] = {}
    for row in cm_rows:
        symbols.setdefault(row.symbol, "individual_securities")
    for row in fo_rows:
        symbol_type = "index" if row.instrument_type == "OPTIDX" else "individual_securities"
        symbols[row.symbol] = symbol_type
    return [{"symbol": symbol, "symbol_type": symbol_type} for symbol, symbol_type in sorted(symbols.items())]
