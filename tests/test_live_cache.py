from __future__ import annotations

import asyncio
import json
from datetime import datetime

import app.api.routes as routes
import app.services.live as live_service
from app.core.config import Settings


def test_symbol_scoped_live_refresh_does_not_replace_aggregate_cache(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.hash_keys: list[str] = []
            self.set_keys: list[str] = []

        async def hset(self, key: str, mapping: dict) -> None:
            self.hash_keys.append(key)

        async def expire(self, key: str, ttl: int) -> None:
            pass

        async def set(self, key: str, value: str, ex: int) -> None:
            json.loads(value)
            self.set_keys.append(key)

    class FakeRepo:
        async def yahoo_symbols_for(self, symbols: list[str]) -> dict[str, str]:
            return {symbol: f"{symbol}.NS" for symbol in symbols}

        async def live_baseline(self, symbols: list[str]) -> dict[str, dict]:
            return {symbol: {"symbol": symbol, "avg_option_volume": 100} for symbol in symbols}

        async def live_forward_factor_percentiles(self, payloads: list[dict]) -> dict:
            return {}

        async def upsert_live_symbol_metrics(self, payloads: list[dict]) -> None:
            self.payloads = payloads

    class FakeYahooClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def fetch_live_quotes(self, symbols: list[str], yahoo_symbols: dict[str, str]) -> dict:
            return {
                symbol: {
                    "symbol": symbol,
                    "provider": "yahoo",
                    "provider_symbol": yahoo_symbols[symbol],
                    "current_price": 123.45,
                }
                for symbol in symbols
            }

    async def no_option_summaries(settings, repo, redis, symbols, baseline) -> dict:
        return {}

    monkeypatch.setattr(live_service, "YahooFinanceClient", FakeYahooClient)
    monkeypatch.setattr(live_service, "_fetch_live_option_summaries", no_option_summaries)

    redis = FakeRedis()
    result = asyncio.run(
        live_service._fetch_and_store_yahoo_live_quotes(
            Settings(live_symbols="ABC"),
            FakeRepo(),  # type: ignore[arg-type]
            redis,  # type: ignore[arg-type]
            ["ABC"],
        )
    )

    assert result["quotes_stored"] == 1
    assert redis.hash_keys == ["live:ABC"]
    assert "live:symbols" not in redis.set_keys


def test_full_live_refresh_updates_aggregate_cache(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.set_keys: list[str] = []

        async def hset(self, key: str, mapping: dict) -> None:
            pass

        async def expire(self, key: str, ttl: int) -> None:
            pass

        async def set(self, key: str, value: str, ex: int) -> None:
            json.loads(value)
            self.set_keys.append(key)

    class FakeRepo:
        async def yahoo_symbols_for(self, symbols: list[str]) -> dict[str, str]:
            return {symbol: f"{symbol}.NS" for symbol in symbols}

        async def live_baseline(self, symbols: list[str]) -> dict[str, dict]:
            return {symbol: {"symbol": symbol} for symbol in symbols}

        async def live_forward_factor_percentiles(self, payloads: list[dict]) -> dict:
            return {}

        async def upsert_live_symbol_metrics(self, payloads: list[dict]) -> None:
            pass

    class FakeYahooClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def fetch_live_quotes(self, symbols: list[str], yahoo_symbols: dict[str, str]) -> dict:
            return {
                symbol: {
                    "symbol": symbol,
                    "provider": "yahoo",
                    "provider_symbol": yahoo_symbols[symbol],
                    "current_price": 123.45,
                }
                for symbol in symbols
            }

    async def no_option_summaries(settings, repo, redis, symbols, baseline) -> dict:
        return {}

    monkeypatch.setattr(live_service, "YahooFinanceClient", FakeYahooClient)
    monkeypatch.setattr(live_service, "_fetch_live_option_summaries", no_option_summaries)

    redis = FakeRedis()
    result = asyncio.run(
        live_service._fetch_and_store_yahoo_live_quotes(
            Settings(live_symbols="ABC"),
            FakeRepo(),  # type: ignore[arg-type]
            redis,  # type: ignore[arg-type]
            None,
        )
    )

    assert result["quotes_stored"] == 1
    assert "live:symbols" in redis.set_keys


def test_live_symbols_uses_database_fallback_when_aggregate_cache_is_empty(monkeypatch) -> None:
    class FakeCache:
        redis = object()

        async def get_live_symbols(self) -> list[dict]:
            return []

    class FakeRepo:
        async def latest_live_metrics(self) -> dict[str, dict]:
            return {
                "ABC": {
                    "symbol": "ABC",
                    "current_price": 123.45,
                    "bid_ask_spread_pct": 4.5,
                    "snapshot_time": "2026-07-08T12:00:00+05:30",
                }
            }

    async def fail_refresh(*args, **kwargs) -> None:
        raise AssertionError("external refresh should not run when DB live metrics exist")

    async def passthrough(symbol: str, payload: dict, repo: object) -> dict:
        return payload

    monkeypatch.setattr(routes, "fetch_and_store_live_quotes", fail_refresh)
    monkeypatch.setattr(routes, "_refresh_live_payload_forward_percentiles", passthrough)

    result = asyncio.run(
        routes.live_symbols(
            Settings(),
            FakeRepo(),  # type: ignore[arg-type]
            FakeCache(),  # type: ignore[arg-type]
        )
    )

    assert result == [
        {
            "symbol": "ABC",
            "current_price": 123.45,
            "bid_ask_spread_pct": 4.5,
            "snapshot_time": "2026-07-08T12:00:00+05:30",
        }
    ]


def test_live_symbols_deduplicates_aggregate_cache_by_latest_snapshot(monkeypatch) -> None:
    class FakeCache:
        redis = object()

        async def get_live_symbols(self) -> list[dict]:
            return [
                {
                    "symbol": "ABC",
                    "current_price": 100,
                    "snapshot_time": "2026-07-08T10:00:00+05:30",
                },
                {
                    "symbol": "XYZ",
                    "current_price": 200,
                    "snapshot_time": "2026-07-08T10:01:00+05:30",
                },
                {
                    "symbol": "ABC",
                    "current_price": 123,
                    "snapshot_time": "2026-07-08T10:02:00+05:30",
                },
            ]

    class FakeRepo:
        async def latest_live_metrics(self) -> dict[str, dict]:
            raise AssertionError("database fallback should not run when cache has live rows")

    async def passthrough(symbol: str, payload: dict, repo: object) -> dict:
        return payload

    monkeypatch.setattr(routes, "_refresh_live_payload_forward_percentiles", passthrough)

    result = asyncio.run(
        routes.live_symbols(
            Settings(),
            FakeRepo(),  # type: ignore[arg-type]
            FakeCache(),  # type: ignore[arg-type]
        )
    )

    assert result == [
        {
            "symbol": "ABC",
            "current_price": 123,
            "snapshot_time": "2026-07-08T10:02:00+05:30",
        },
        {
            "symbol": "XYZ",
            "current_price": 200,
            "snapshot_time": "2026-07-08T10:01:00+05:30",
        },
    ]


def test_live_payload_merge_does_not_replace_newer_database_row_with_stale_cache() -> None:
    class FakeCache:
        async def get_live_symbols(self) -> list[dict]:
            return [
                {
                    "symbol": "ABC",
                    "current_price": 100,
                    "snapshot_time": "2026-07-23T09:00:00+05:30",
                }
            ]

    class FakeRepo:
        async def latest_live_metrics(self, symbols=None) -> dict[str, dict]:
            return {
                "ABC": {
                    "symbol": "ABC",
                    "current_price": 123,
                    "snapshot_time": "2026-07-23T09:01:00+05:30",
                }
            }

    result = asyncio.run(
        routes._live_payloads_by_symbol(  # type: ignore[attr-defined]
            FakeCache(),  # type: ignore[arg-type]
            FakeRepo(),  # type: ignore[arg-type]
        )
    )

    assert result["ABC"]["current_price"] == 123


def test_latest_live_payload_rejects_row_older_than_eod_baseline() -> None:
    class FakeCache:
        async def get_live(self, symbol: str) -> dict:
            return {
                "symbol": symbol,
                "trade_date": "2026-07-09",
                "snapshot_time": "2026-07-09T09:21:00+05:30",
                "iv_30": 0.50,
            }

    class FakeRepo:
        async def latest_live_metrics(self, symbols: list[str]) -> dict[str, dict]:
            raise AssertionError("cache supplied the live row")

    result = asyncio.run(
        routes._latest_live_payload(  # type: ignore[attr-defined]
            "NIFTY",
            FakeCache(),  # type: ignore[arg-type]
            FakeRepo(),  # type: ignore[arg-type]
            "2026-07-22",
        )
    )

    assert result == {}


def test_live_worker_cycle_processes_indexes_first_in_batches(monkeypatch) -> None:
    batches: list[list[str]] = []

    class FakeRedis:
        def __init__(self) -> None:
            self.aggregate: list[dict] | None = None

        async def set(self, key: str, value: str, ex: int) -> None:
            assert key == "live:symbols"
            self.aggregate = json.loads(value)

    class FakeRepo:
        async def active_symbols(self) -> list[str]:
            return ["RELIANCE", "NIFTY", "ABC", "BANKNIFTY", "MARUTI"]

        async def latest_live_metrics(self, symbols: list[str]) -> dict[str, dict]:
            return {
                symbol: {
                    "symbol": symbol,
                    "snapshot_time": "2026-07-23T09:01:00+05:30",
                }
                for symbol in symbols
            }

    async def fake_fetch(settings, repo, redis, symbols) -> dict:
        batches.append(symbols)
        return {"quotes_stored": len(symbols)}

    monkeypatch.setattr(live_service, "fetch_and_store_live_quotes", fake_fetch)
    redis = FakeRedis()
    result = asyncio.run(
        live_service.run_live_worker_cycle(
            Settings(
                live_symbols="all",
                live_worker_batch_size=2,
                live_kite_quote_batch_delay_seconds=0,
            ),
            FakeRepo(),  # type: ignore[arg-type]
            redis,  # type: ignore[arg-type]
        )
    )

    assert batches == [
        ["BANKNIFTY", "NIFTY"],
        ["ABC", "MARUTI"],
        ["RELIANCE"],
    ]
    assert result == {"symbols_requested": 5, "batches": 3, "quotes_stored": 5}
    assert redis.aggregate is not None
    assert [item["symbol"] for item in redis.aggregate] == [
        "ABC",
        "BANKNIFTY",
        "MARUTI",
        "NIFTY",
        "RELIANCE",
    ]


def test_market_window_blocks_worker_after_close_and_weekends() -> None:
    settings = Settings(live_market_start_ist="09:00", live_market_end_ist="16:00")

    assert live_service.in_market_window(
        settings,
        datetime(2026, 7, 10, 15, 59, tzinfo=live_service.IST),
    )
    assert not live_service.in_market_window(
        settings,
        datetime(2026, 7, 10, 16, 1, tzinfo=live_service.IST),
    )
    assert not live_service.in_market_window(
        settings,
        datetime(2026, 7, 11, 10, 0, tzinfo=live_service.IST),
    )
