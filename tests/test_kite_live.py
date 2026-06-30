from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import math

import app.services.live as live_service
from app.core.config import Settings
from app.services.calculations import black_scholes_price
from app.sources.kite import normalize_market_quotes, session_checksum


def test_kite_session_checksum_is_sha256_of_key_token_secret() -> None:
    assert (
        session_checksum("api_key", "request_token", "api_secret")
        == "ff6a6d3d60c9d974df906ba6f787ac38300cfa68b41801b486ea1007e52e8942"
    )


def test_kite_market_quote_normalization() -> None:
    payload = {
        "data": {
            "NSE:RELIANCE": {
                "instrument_token": 738561,
                "last_price": 2900.5,
                "volume": 123456,
                "oi": 0,
                "ohlc": {"open": 2880, "high": 2910, "low": 2870, "close": 2865},
            }
        }
    }

    quotes = normalize_market_quotes(payload, {"RELIANCE": "NSE:RELIANCE"})

    assert quotes["RELIANCE"]["provider"] == "kite"
    assert quotes["RELIANCE"]["provider_symbol"] == "NSE:RELIANCE"
    assert quotes["RELIANCE"]["current_price"] == 2900.5
    assert quotes["RELIANCE"]["volume"] == 123456


def test_kite_option_summary_calculates_call_put_iv_from_quote_prices() -> None:
    trade_date = date(2026, 6, 1)
    expiry = trade_date + timedelta(days=30)
    spot = 100.0
    strike = 100.0
    rate = 0.06
    call_price = black_scholes_price(spot, strike, 30 / 365, rate, 0.25, "CE")
    put_price = black_scholes_price(spot, strike, 30 / 365, rate, 0.20, "PE")
    request = {
        "symbol": "ABC",
        "spot": spot,
        "expiry": expiry,
        "strike": strike,
        "strike_count": 3,
        "ce_key": "NFO:ABC26JUN100CE",
        "pe_key": "NFO:ABC26JUN100PE",
    }
    quotes = {
        "data": {
            "NFO:ABC26JUN100CE": _quote(call_price, 10),
            "NFO:ABC26JUN100PE": _quote(put_price, 20),
        }
    }

    summary = live_service._kite_option_summary_from_quotes(request, quotes, trade_date, rate)

    assert summary is not None
    assert summary["provider"] == "kite"
    assert summary["live_option_volume"] == 30
    assert summary["live_option_volume_kind"] == "atm_contracts_call_plus_put"
    assert summary["live_atm_strike"] == 100.0
    assert math.isclose(summary["live_atm_call_iv"], 0.25, rel_tol=1e-5)
    assert math.isclose(summary["live_atm_put_iv"], 0.20, rel_tol=1e-5)
    assert math.isclose(summary["live_atm_iv"], 0.225, rel_tol=1e-5)
    assert summary["live_atm_iv_source"] == "kite:quote:calculated-iv"


def test_kite_option_summary_prefers_bid_ask_mid_over_ltp_for_iv() -> None:
    trade_date = date(2026, 6, 1)
    expiry = trade_date + timedelta(days=30)
    spot = 100.0
    strike = 100.0
    rate = 0.06
    call_mid = black_scholes_price(spot, strike, 30 / 365, rate, 0.25, "CE")
    put_mid = black_scholes_price(spot, strike, 30 / 365, rate, 0.20, "PE")
    stale_ltp = black_scholes_price(spot, strike, 30 / 365, rate, 0.60, "CE")
    request = {
        "symbol": "ABC",
        "spot": spot,
        "expiry": expiry,
        "strike": strike,
        "strike_count": 3,
        "ce_key": "NFO:ABC26JUN100CE",
        "pe_key": "NFO:ABC26JUN100PE",
    }
    quotes = {
        "data": {
            "NFO:ABC26JUN100CE": _quote_with_depth(stale_ltp, call_mid - 0.05, call_mid + 0.05, 10),
            "NFO:ABC26JUN100PE": _quote_with_depth(stale_ltp, put_mid - 0.05, put_mid + 0.05, 20),
        }
    }

    summary = live_service._kite_option_summary_from_quotes(request, quotes, trade_date, rate)

    assert summary is not None
    assert math.isclose(summary["live_atm_call_iv"], 0.25, rel_tol=1e-5)
    assert math.isclose(summary["live_atm_put_iv"], 0.20, rel_tol=1e-5)
    assert math.isclose(summary["live_atm_iv"], 0.225, rel_tol=1e-5)


def test_kite_option_summary_ignores_stale_ltp_without_depth_or_volume() -> None:
    trade_date = date(2026, 6, 1)
    expiry = trade_date + timedelta(days=30)
    request = {
        "symbol": "ABC",
        "spot": 100.0,
        "expiry": expiry,
        "strike": 100.0,
        "strike_count": 3,
        "ce_key": "NFO:ABC26JUN100CE",
        "pe_key": "NFO:ABC26JUN100PE",
    }
    stale_quote = {
        "last_price": 25.0,
        "volume": 0,
        "oi": 100,
        "depth": {"buy": [], "sell": []},
    }
    quotes = {
        "data": {
            "NFO:ABC26JUN100CE": stale_quote,
            "NFO:ABC26JUN100PE": stale_quote,
        }
    }

    summary = live_service._kite_option_summary_from_quotes(request, quotes, trade_date, 0.06)

    assert summary is None


def test_kite_expiry_targets_are_distinct_when_nearest_targets_overlap() -> None:
    trade_date = date(2026, 6, 25)
    rows = [
        {"expiry": date(2026, 7, 28)},
        {"expiry": date(2026, 8, 25)},
    ]

    targets = live_service._kite_expiry_targets(rows, trade_date)

    assert targets == [date(2026, 7, 28), date(2026, 8, 25)]


def test_kite_expiry_targets_skip_expiry_day_contracts() -> None:
    trade_date = date(2026, 6, 30)
    rows = [
        {"expiry": date(2026, 6, 30)},
        {"expiry": date(2026, 7, 28)},
        {"expiry": date(2026, 8, 25)},
        {"expiry": date(2026, 9, 29)},
    ]

    targets = live_service._kite_expiry_targets(rows, trade_date)

    assert targets == [
        date(2026, 7, 28),
        date(2026, 8, 25),
        date(2026, 9, 29),
    ]


def test_future_expiry_targets_from_baseline_skip_expiry_day_and_backfill() -> None:
    trade_date = date(2026, 6, 30)
    base = {
        "expiry_30d": date(2026, 6, 30),
        "expiry_60d": date(2026, 7, 28),
        "expiry_90d": date(2026, 8, 25),
    }
    fallback = [date(2026, 7, 28), date(2026, 8, 25), date(2026, 9, 29)]

    targets = live_service._merge_expiry_targets(
        live_service._future_expiry_targets_from_baseline(base, trade_date),
        fallback,
    )

    assert targets == [
        date(2026, 7, 28),
        date(2026, 8, 25),
        date(2026, 9, 29),
    ]


def test_kite_option_request_uses_preferred_same_strike_for_far_expiry() -> None:
    expiry = date(2026, 7, 28)
    rows = [
        _instrument(expiry, 100.0, "CE"),
        _instrument(expiry, 100.0, "PE"),
        _instrument(expiry, 105.0, "CE"),
        _instrument(expiry, 105.0, "PE"),
    ]

    request = live_service._kite_atm_option_request("ABC", 104.0, rows, expiry, 100.0)

    assert request is not None
    assert request["strike"] == 100.0
    assert request["ce_key"] == "NFO:ABC100CE"
    assert request["pe_key"] == "NFO:ABC100PE"


def test_live_quote_payload_clears_absent_far_tenor_fields() -> None:
    now = datetime(2026, 6, 25, 10, 45, tzinfo=live_service.IST)
    base = {
        "symbol": "ABC",
        "expiry_90d": date(2026, 9, 24),
        "dte_90": 91,
        "iv_90": 0.30,
    }
    quote = {"symbol": "ABC", "provider": "kite", "current_price": 100.0}
    option_summary = {
        "provider": "kite",
        "live_option_volume": 30,
        "live_option_volume_source": "kite:quote",
        "live_option_volume_kind": "atm_contracts_call_plus_put",
        "live_atm_iv_source": "kite:quote:calculated-iv",
        "live_iv_terms": [
            {"expiry_date": date(2026, 7, 25), "call_iv": 0.20, "put_iv": 0.22},
            {"expiry_date": date(2026, 8, 24), "call_iv": 0.25, "put_iv": 0.27},
        ],
    }

    payload = live_service._live_quote_payload(base, quote, option_summary, now)

    assert payload["expiry_90d"] is None
    assert payload["dte_90"] is None
    assert payload["iv_90"] is None
    assert payload["eod_iv_90"] == 0.30
    assert payload["live_raw_call_iv_term_structure"] == [
        {"tenor": 30, "dte": 30, "expiry": "2026-07-25", "iv": 0.20},
        {"tenor": 60, "dte": 60, "expiry": "2026-08-24", "iv": 0.25},
    ]
    assert payload["live_raw_put_iv_term_structure"] == [
        {"tenor": 30, "dte": 30, "expiry": "2026-07-25", "iv": 0.22},
        {"tenor": 60, "dte": 60, "expiry": "2026-08-24", "iv": 0.27},
    ]


def test_kite_token_refresh_logs_missing_request_token_once() -> None:
    class Repo:
        def __init__(self) -> None:
            self.logs: list[tuple[str, str, str]] = []

        async def log_error(self, task_name: str, error_type: str, details: dict, source: str) -> None:
            self.logs.append((task_name, error_type, source))
            assert "login_url" in details

    repo = Repo()
    live_service._KITE_TOKEN_REFRESH_LAST_DATE = None
    settings = Settings(
        live_quote_provider="kite",
        kite_api_key="key",
        kite_auto_refresh_enabled=True,
        kite_token_refresh_time_ist="06:05",
    )
    now = datetime(2026, 6, 25, 6, 5, tzinfo=live_service.IST)

    asyncio.run(live_service._maybe_refresh_kite_access_token(settings, repo, object(), now))
    asyncio.run(live_service._maybe_refresh_kite_access_token(settings, repo, object(), now))

    assert repo.logs == [("kite_token_refresh", "MissingRequestToken", "kite:auth")]


def test_generate_kite_access_token_stores_token_in_repo_and_redis(monkeypatch) -> None:
    async def fake_generate_session(*args, **kwargs) -> dict:
        return {
            "user_id": "WK7754",
            "api_key": "key",
            "access_token": "access",
            "login_time": "2026-06-25 06:06:00",
        }

    class Repo:
        def __init__(self) -> None:
            self.token: tuple[str, str, datetime | None, dict] | None = None

        async def upsert_broker_access_token(
            self,
            provider: str,
            access_token: str,
            expires_at: datetime | None,
            payload: dict,
        ) -> None:
            self.token = (provider, access_token, expires_at, payload)

    class Redis:
        def __init__(self) -> None:
            self.value: tuple[str, str, int] | None = None

        async def set(self, key: str, value: str, ex: int) -> None:
            self.value = (key, value, ex)

    monkeypatch.setattr(live_service.KiteConnectClient, "generate_session", fake_generate_session)
    repo = Repo()
    redis = Redis()

    payload = asyncio.run(
        live_service.generate_kite_access_token(
            Settings(kite_api_key="key", kite_api_secret="secret"),
            repo,  # type: ignore[arg-type]
            redis,  # type: ignore[arg-type]
            "request",
        )
    )

    assert payload["access_token"] == "access"
    assert payload["expires_at"].startswith("2026-06-26T06:00:00")
    assert repo.token is not None
    assert repo.token[0] == "kite"
    assert repo.token[1] == "access"
    assert redis.value is not None
    assert redis.value[0] == "kite:access-token:key"


def test_kite_option_summary_provider_falls_back_to_nse_on_failure(monkeypatch) -> None:
    async def kite_failure(settings: Settings, repo: object, redis: object, symbols: list[str], baseline: dict) -> dict:
        raise RuntimeError("kite token missing")

    async def nse_success(settings: Settings, symbols: list[str], baseline: dict) -> dict:
        return {"ABC": {"provider": "nse", "live_option_volume": 20}}

    class Repo:
        async def log_error(self, task_name: str, error_type: str, details: dict, source: str) -> None:
            assert task_name == "live_option_summary_provider_fallback"
            assert error_type == "RuntimeError"
            assert details["fallback_provider"] == "nse"
            assert source == "kite:fallback_to_nse"

    monkeypatch.setattr(live_service, "_fetch_kite_live_option_summaries", kite_failure)
    monkeypatch.setattr(live_service, "_fetch_nse_live_option_summaries", nse_success)

    result = asyncio.run(
        live_service._fetch_live_option_summaries(
            Settings(live_option_summary_provider="kite"),
            Repo(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ["ABC"],
            {},
        )
    )

    assert result == {"ABC": {"provider": "nse", "live_option_volume": 20}}


def test_kite_quote_provider_falls_back_to_yahoo_on_failure(monkeypatch) -> None:
    async def kite_failure(settings: Settings, repo: object, redis: object, symbols: list[str] | None) -> dict:
        raise RuntimeError("kite token missing")

    async def yahoo_success(settings: Settings, repo: object, redis: object, symbols: list[str] | None) -> dict:
        return {"provider": "yahoo", "quotes_stored": 1}

    class Repo:
        async def log_error(self, task_name: str, error_type: str, details: dict, source: str) -> None:
            assert task_name == "live_quote_provider_fallback"
            assert error_type == "RuntimeError"
            assert details["fallback_provider"] == "yahoo"
            assert source == "kite:fallback_to_yahoo"

    monkeypatch.setattr(live_service, "_fetch_and_store_kite_live_quotes", kite_failure)
    monkeypatch.setattr(live_service, "_fetch_and_store_yahoo_live_quotes", yahoo_success)

    result = asyncio.run(
        live_service.fetch_and_store_live_quotes(
            Settings(live_quote_provider="kite"),
            Repo(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            ["ABC"],
        )
    )

    assert result == {"provider": "yahoo", "quotes_stored": 1}


def _quote(price: float, volume: int) -> dict:
    return {
        "last_price": price,
        "volume": volume,
        "oi": volume * 10,
        "depth": {
            "buy": [{"price": price}],
            "sell": [{"price": price}],
        },
    }


def _quote_with_depth(last_price: float, bid: float, ask: float, volume: int) -> dict:
    return {
        "last_price": last_price,
        "volume": volume,
        "oi": volume * 10,
        "depth": {
            "buy": [{"price": bid}],
            "sell": [{"price": ask}],
        },
    }


def _instrument(expiry: date, strike: float, option_type: str) -> dict:
    return {
        "exchange": "NFO",
        "segment": "NFO-OPT",
        "name": "ABC",
        "instrument_type": option_type,
        "expiry": expiry,
        "strike": strike,
        "tradingsymbol": f"ABC{int(strike)}{option_type}",
    }
