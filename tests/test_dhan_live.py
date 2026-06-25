from __future__ import annotations

import asyncio
import math
from datetime import date

import app.services.live as live_service
from app.api.routes import _overlay_live_history, _overlay_live_term_structure
from app.core.config import Settings
from app.sources.dhan import (
    combine_expiry_summaries,
    current_totp,
    normalize_option_chain_summary,
    token_expiry,
)


def test_current_totp_uses_standard_totp_vector_truncated_to_six_digits() -> None:
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    assert current_totp(secret, timestamp=59) == "287082"


def test_token_expiry_parses_dhan_expiry_time() -> None:
    expiry = token_expiry({"expiryTime": "2026-01-01T00:00:00.000"})

    assert expiry is not None
    assert expiry.isoformat() == "2026-01-01T00:00:00"


def test_dhan_option_chain_summary_normalizes_atm_iv_and_volume() -> None:
    payload = {
        "data": {
            "last_price": 1365,
            "oc": {
                "1360.000000": {
                    "ce": {"implied_volatility": 20, "last_price": 12.5, "volume": 10, "oi": 100},
                    "pe": {"implied_volatility": 30, "last_price": 10.5, "volume": 20, "oi": 200},
                },
                "1370.000000": {
                    "ce": {"implied_volatility": 21, "last_price": 9.5, "volume": 30, "oi": 300},
                    "pe": {"implied_volatility": 31, "last_price": 11.5, "volume": 40, "oi": 400},
                },
            },
        }
    }

    summary = normalize_option_chain_summary("RELIANCE", date(2026, 5, 26), payload)

    assert summary is not None
    assert summary["provider"] == "dhan"
    assert summary["live_option_volume"] == 100
    assert summary["live_option_volume_source"] == "dhan:optionchain"
    assert summary["live_option_expiry"] == "2026-05-26"
    assert summary["live_option_expiry_date"] == date(2026, 5, 26)
    assert summary["live_option_underlying"] == 1365
    assert summary["live_atm_strike"] == 1360
    assert summary["live_atm_iv"] == 0.25
    assert summary["live_atm_call_iv"] == 0.20
    assert summary["live_atm_put_iv"] == 0.30
    assert summary["live_atm_option_volume"] == 30
    assert summary["live_atm_call_oi"] == 100
    assert summary["live_atm_put_oi"] == 200


def test_dhan_expiry_summaries_preserve_all_live_iv_terms() -> None:
    first = {
        "symbol": "RELIANCE",
        "provider": "dhan",
        "live_option_volume": 100,
        "live_option_expiry": "2026-06-25",
        "live_option_expiry_date": date(2026, 6, 25),
        "live_atm_strike": 1000,
        "live_atm_iv": 0.20,
        "live_atm_call_iv": 0.21,
        "live_atm_put_iv": 0.19,
    }
    second = {
        **first,
        "live_option_expiry": "2026-07-30",
        "live_option_expiry_date": date(2026, 7, 30),
        "live_atm_iv": 0.25,
        "live_atm_call_iv": 0.26,
        "live_atm_put_iv": 0.24,
    }

    combined = combine_expiry_summaries("RELIANCE", [first, second])

    assert combined is not None
    assert combined["live_iv_term_count"] == 2
    assert [item["expiry"] for item in combined["live_iv_terms"]] == ["2026-06-25", "2026-07-30"]
    assert [item["atm_iv"] for item in combined["live_iv_terms"]] == [0.20, 0.25]


def test_live_forward_metrics_use_dhan_source_markers() -> None:
    summary = {
        "live_atm_iv_source": "dhan:optionchain",
        "live_iv_terms": [
            {"expiry_date": date(2026, 6, 27), "atm_iv": 0.20, "call_iv": 0.22, "put_iv": 0.18},
            {"expiry_date": date(2026, 7, 27), "atm_iv": 0.25, "call_iv": 0.26, "put_iv": 0.24},
        ],
    }

    metrics = live_service._live_forward_metrics(summary, date(2026, 5, 28))

    expected_fwdv = math.sqrt((0.25**2 * 60 - 0.20**2 * 30) / 30)
    assert math.isclose(metrics["fwdv_3060"], expected_fwdv)
    assert metrics["iv_term_structure_source"] == "dhan:optionchain"
    assert metrics["forward_analytics_source"] == "dhan:optionchain"
    assert metrics["iv_30_source"] == "dhan:optionchain"


def test_dhan_option_summaries_fall_back_to_nse_for_missing_symbols(monkeypatch) -> None:
    async def dhan_partial(settings: Settings, redis: object, symbols: list[str], baseline: dict) -> dict:
        return {"AAA": {"provider": "dhan", "live_option_volume": 10}}

    async def nse_for_missing(settings: Settings, symbols: list[str], baseline: dict) -> dict:
        assert symbols == ["BBB"]
        return {"BBB": {"provider": "nse", "live_option_volume": 20}}

    class Repo:
        async def log_error(self, *args, **kwargs) -> None:
            raise AssertionError("partial Dhan success should not log a provider fallback")

    monkeypatch.setattr(live_service, "_fetch_dhan_live_option_summaries", dhan_partial)
    monkeypatch.setattr(live_service, "_fetch_nse_live_option_summaries", nse_for_missing)

    result = asyncio.run(
        live_service._fetch_live_option_summaries(
            Settings(live_option_summary_provider="dhan"),
            Repo(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ["AAA", "BBB"],
            {},
        )
    )

    assert result == {
        "AAA": {"provider": "dhan", "live_option_volume": 10},
        "BBB": {"provider": "nse", "live_option_volume": 20},
    }


def test_dhan_option_summaries_fall_back_to_nse_on_provider_failure(monkeypatch) -> None:
    calls: list[str] = []

    async def dhan_failure(settings: Settings, redis: object, symbols: list[str], baseline: dict) -> dict:
        raise RuntimeError("token expired")

    async def nse_success(settings: Settings, symbols: list[str], baseline: dict) -> dict:
        calls.append(",".join(symbols))
        return {"AAA": {"provider": "nse", "live_option_volume": 20}}

    class Repo:
        async def log_error(self, task_name: str, error_type: str, details: dict, source: str) -> None:
            assert task_name == "live_option_summary_provider_fallback"
            assert error_type == "RuntimeError"
            assert details["fallback_provider"] == "nse"
            assert source == "dhan:fallback_to_nse"

    monkeypatch.setattr(live_service, "_fetch_dhan_live_option_summaries", dhan_failure)
    monkeypatch.setattr(live_service, "_fetch_nse_live_option_summaries", nse_success)

    result = asyncio.run(
        live_service._fetch_live_option_summaries(
            Settings(live_option_summary_provider="dhan"),
            Repo(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ["AAA"],
            {},
        )
    )

    assert calls == ["AAA"]
    assert result == {"AAA": {"provider": "nse", "live_option_volume": 20}}


def test_detail_history_overlay_appends_latest_live_row() -> None:
    history = [{"trade_date": "2026-05-28", "iv_30": 0.18}]
    live = {
        "snapshot_time": "2026-05-29T15:29:00+05:30",
        "iv_30": 0.20,
        "iv_60": 0.25,
        "avg_option_volume": 12345,
        "live_atm_strike": 1000,
        "iv_term_structure_source": "dhan:optionchain",
    }

    overlaid = _overlay_live_history(history, live)

    assert overlaid[-1]["trade_date"] == "2026-05-29"
    assert overlaid[-1]["is_live"] is True
    assert overlaid[-1]["iv_30"] == 0.20
    assert overlaid[-1]["avg_option_volume"] == 12345
    assert overlaid[-1]["live_atm_strike"] == 1000
    assert history == [{"trade_date": "2026-05-28", "iv_30": 0.18}]


def test_term_structure_overlay_accepts_dhan_source() -> None:
    result = {
        "current": {"trade_date": "2026-05-28", "iv_30": 0.18},
        "history": [{"trade_date": "2026-05-28", "iv_30": 0.18}],
    }
    live = {
        "snapshot_time": "2026-05-29T15:29:00+05:30",
        "iv_term_structure_source": "dhan:optionchain",
        "iv_30": 0.20,
        "iv_60": 0.25,
        "dte_30": 27,
    }

    overlaid = _overlay_live_term_structure(result, live)

    assert overlaid["current"]["is_live"] is True
    assert overlaid["current"]["iv_term_structure_source"] == "dhan:optionchain"
    assert overlaid["current"]["iv_30"] == 0.20
    assert overlaid["history"][-1]["dte_30"] == 27


def test_dhan_quote_provider_falls_back_to_yahoo_on_failure(monkeypatch) -> None:
    calls: list[str] = []

    async def dhan_failure(settings: Settings, repo: object, redis: object, symbols: list[str] | None) -> dict:
        raise RuntimeError("dhan rate limited")

    async def yahoo_success(settings: Settings, repo: object, redis: object, symbols: list[str] | None) -> dict:
        calls.append(",".join(symbols or []))
        return {"provider": "yahoo", "quotes_stored": 1}

    class Repo:
        async def log_error(self, task_name: str, error_type: str, details: dict, source: str) -> None:
            assert task_name == "live_quote_provider_fallback"
            assert error_type == "RuntimeError"
            assert details["fallback_provider"] == "yahoo"
            assert source == "dhan:fallback_to_yahoo"

    monkeypatch.setattr(live_service, "_fetch_and_store_dhan_live_quotes", dhan_failure)
    monkeypatch.setattr(live_service, "_fetch_and_store_yahoo_live_quotes", yahoo_success)

    result = asyncio.run(
        live_service.fetch_and_store_live_quotes(
            Settings(live_quote_provider="dhan"),
            Repo(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ["AAA"],
        )
    )

    assert calls == ["AAA"]
    assert result == {"provider": "yahoo", "quotes_stored": 1}
