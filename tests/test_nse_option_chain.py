from __future__ import annotations

import asyncio
import math
from datetime import date

import app.services.live as live_service
from app.core.config import Settings
from app.services.live import _live_forward_metrics, selected_live_symbols
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
    assert summary["live_atm_call_iv"] == 0.20
    assert summary["live_atm_put_iv"] == 0.30


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
            {"expiry_date": date(2026, 6, 27), "atm_iv": 0.20, "call_iv": 0.22, "put_iv": 0.18},
            {"expiry_date": date(2026, 7, 27), "atm_iv": 0.25, "call_iv": 0.26, "put_iv": 0.24},
            {"expiry_date": date(2026, 8, 26), "atm_iv": 0.30, "call_iv": 0.31, "put_iv": 0.29},
        ]
    }

    metrics = _live_forward_metrics(summary, date(2026, 5, 28))

    assert metrics["iv_30"] == 0.20
    assert metrics["iv_60"] == 0.25
    assert metrics["iv_90"] == 0.30
    expected_fwdv = math.sqrt((0.25**2 * 60 - 0.20**2 * 30) / 30)
    assert math.isclose(metrics["fwdv_3060"], expected_fwdv)
    assert math.isclose(metrics["fwdfct_3060"], (0.20 / expected_fwdv) - 1.0)
    expected_call_fwdv = math.sqrt((0.26**2 * 60 - 0.22**2 * 30) / 30)
    expected_put_fwdv = math.sqrt((0.24**2 * 60 - 0.18**2 * 30) / 30)
    assert math.isclose(metrics["call_fwdfct_3060"], 0.22 / expected_call_fwdv - 1.0)
    assert math.isclose(metrics["put_fwdfct_3060"], 0.18 / expected_put_fwdv - 1.0)
    assert math.isclose(metrics["iv_slope_3060"], (0.25 - 0.20) / 30)
    assert metrics["iv_term_structure_source"] == "nse:option-chain-v3"


def test_live_forward_metrics_do_not_invent_missing_far_tenor() -> None:
    summary = {
        "live_iv_terms": [
            {"expiry_date": date(2026, 6, 30), "call_iv": 0.19, "put_iv": 0.21},
        ]
    }

    metrics = _live_forward_metrics(summary, date(2026, 5, 28))

    assert metrics["iv_30"] == 0.20
    assert metrics["iv_60"] is None
    assert metrics["iv_90"] is None
    assert metrics["fwdv_3060"] is None
    assert metrics["fwdfct_3060"] is None


def test_live_forward_metrics_bucket_expiries_as_30_60_90() -> None:
    summary = {
        "live_atm_iv_source": "kite:quote:calculated-iv",
        "live_iv_terms": [
            {
                "expiry_date": date(2026, 6, 30),
                "call_iv": 0.1530,
                "put_iv": 0.2288,
            },
            {
                "expiry_date": date(2026, 7, 28),
                "call_iv": 0.2163,
                "put_iv": 0.2415,
            },
        ],
    }

    metrics = _live_forward_metrics(summary, date(2026, 6, 25))

    assert math.isclose(metrics["iv_30"], (0.1530 + 0.2288) / 2)
    assert math.isclose(metrics["iv_60"], (0.2163 + 0.2415) / 2)
    expected_call_fwdv = math.sqrt((0.2163**2 * 33 - 0.1530**2 * 5) / (33 - 5))
    expected_put_fwdv = math.sqrt((0.2415**2 * 33 - 0.2288**2 * 5) / (33 - 5))
    assert math.isclose(metrics["call_fwdfct_3060"], 0.1530 / expected_call_fwdv - 1.0)
    assert math.isclose(metrics["put_fwdfct_3060"], 0.2288 / expected_put_fwdv - 1.0)
    expected_fwdv = math.sqrt(
        (metrics["iv_60"] ** 2 * 33 - metrics["iv_30"] ** 2 * 5) / (33 - 5)
    )
    assert math.isclose(metrics["fwdv_3060"], expected_fwdv)
    assert metrics["iv_90"] is None
    assert metrics["dte_30"] == 5
    assert metrics["dte_60"] == 33
    assert metrics["fwdv_3060"] is not None
    assert metrics["fwdfct_3060"] is not None
    assert metrics["call_fwdfct_3060"] is not None
    assert metrics["put_fwdfct_3060"] is not None
    assert math.isclose(metrics["iv_slope_3060"], (metrics["iv_60"] - metrics["iv_30"]) / (33 - 5))


def test_live_forward_metrics_require_both_sides_for_average_factor() -> None:
    summary = {
        "live_iv_terms": [
            {
                "expiry_date": date(2026, 6, 30),
                "atm_iv": 0.275,
                "call_iv": 0.20,
                "put_iv": 0.31,
            },
            {
                "expiry_date": date(2026, 7, 28),
                "atm_iv": 0.20,
                "call_iv": 0.30,
                "put_iv": None,
            },
            {
                "expiry_date": date(2026, 8, 25),
                "atm_iv": 0.22,
                "call_iv": 0.35,
                "put_iv": None,
            },
        ]
    }

    metrics = _live_forward_metrics(summary, date(2026, 6, 25))

    assert metrics["fwdfct_3060"] is None
    assert metrics["put_fwdfct_3060"] is None
    assert metrics["call_fwdfct_3060"] is not None
    assert metrics["iv_30"] is not None
    assert metrics["iv_60"] is None
    assert metrics["call_iv_30"] is not None


def test_live_snapshot_falls_back_to_nse_when_dhan_fails(monkeypatch) -> None:
    calls: list[tuple[str, list[str] | None]] = []

    class Repo:
        async def log_error(self, task_name: str, error_type: str, details: dict, source: str) -> None:
            calls.append((source, details["symbols"]))

    async def dhan_failure(settings: Settings, repo: Repo, redis: object, symbols: list[str] | None) -> dict:
        raise RuntimeError("dhan unauthorized")

    async def nse_success(settings: Settings, repo: Repo, redis: object, symbols: list[str] | None) -> dict:
        calls.append(("nse", symbols))
        return {"symbols_requested": len(symbols or []), "snapshots_stored": len(symbols or [])}

    monkeypatch.setattr(live_service, "_fetch_and_store_dhan_live_snapshots", dhan_failure)
    monkeypatch.setattr(live_service, "_fetch_and_store_nse_live_snapshots", nse_success)

    result = asyncio.run(
        live_service.fetch_and_store_live_snapshots(
            Settings(live_option_chain_provider="dhan", dhan_client_id="id", dhan_access_token="token"),
            Repo(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ["RELIANCE"],
        )
    )

    assert result == {"symbols_requested": 1, "snapshots_stored": 1}
    assert calls == [("dhan:fallback_to_nse", ["RELIANCE"]), ("nse", ["RELIANCE"])]


def test_selected_live_symbols_all_uses_active_universe() -> None:
    class Repo:
        async def active_symbols(self) -> list[str]:
            return ["RELIANCE", "SBIN"]

    result = asyncio.run(selected_live_symbols(Settings(live_symbols="all"), Repo()))  # type: ignore[arg-type]

    assert result == ["RELIANCE", "SBIN"]


def test_selected_live_symbols_comma_list_limits_universe() -> None:
    class Repo:
        async def active_symbols(self) -> list[str]:
            raise AssertionError("active universe should not be loaded")

    result = asyncio.run(selected_live_symbols(Settings(live_symbols="reliance, sbin"), Repo()))  # type: ignore[arg-type]

    assert result == ["RELIANCE", "SBIN"]
