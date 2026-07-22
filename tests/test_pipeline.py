from __future__ import annotations

import asyncio
from datetime import date

from app.core.config import Settings
from app.etl.pipeline import Pipeline, _discovered_symbols
from app.sources.models import EquityBar, EquityBhavcopyRow, OptionBhavcopyRow


class FakeBhavcopySource:
    async def fetch_fo(self, trade_date: date) -> list[OptionBhavcopyRow]:
        return [
            OptionBhavcopyRow(
                symbol="RELIANCE",
                trade_date=trade_date,
                expiry_date=date(2026, 7, 28),
                strike_price=1280.0,
                option_type="CE",
                instrument_type="OPTSTK",
                open=10.0,
                high=12.0,
                low=9.0,
                close=11.0,
                settle_price=11.0,
                num_contracts=100,
                contract_value=1_000.0,
                open_interest=1_000,
                change_in_oi=10,
                source="unit",
            )
        ]

    async def fetch_cm(self, trade_date: date) -> list[EquityBhavcopyRow]:
        return [
            EquityBhavcopyRow(
                symbol="RELIANCE",
                trade_date=trade_date,
                open=1280.0,
                high=1290.0,
                low=1270.0,
                close=1285.0,
                volume=1_000_000,
                turnover=1_285_000_000.0,
                delivery_volume=None,
                source="unit",
            )
        ]


class FakeRates:
    async def fetch_91d_rate(self, start: date, end: date) -> list[dict]:
        return []


def test_discovered_symbols_only_activates_fo_universe() -> None:
    trade_date = date(2026, 7, 21)
    fo_rows = asyncio.run(FakeBhavcopySource().fetch_fo(trade_date))
    cm_rows = asyncio.run(FakeBhavcopySource().fetch_cm(trade_date))
    cm_rows.append(
        EquityBhavcopyRow(
            symbol="CASHONLY",
            trade_date=trade_date,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000,
            turnover=100_500.0,
            delivery_volume=None,
            source="unit",
        )
    )

    assert _discovered_symbols(fo_rows, cm_rows) == [
        {
            "symbol": "CASHONLY",
            "symbol_type": "individual_securities",
            "is_active": False,
        },
        {
            "symbol": "RELIANCE",
            "symbol_type": "individual_securities",
            "is_active": True,
        },
    ]


class FailingCorporateActions:
    async def fetch_actions(self, start: date, end: date, symbols: list[str] | None) -> list[dict]:
        raise RuntimeError("NSE corporate actions blocked")


class FakeRepository:
    async def upsert_option_rows(self, rows: list[OptionBhavcopyRow]) -> int:
        return len(rows)

    async def upsert_equity_rows(self, rows: list[EquityBhavcopyRow]) -> int:
        return len(rows)

    async def upsert_discovered_symbols(self, rows: list[dict]) -> int:
        return len(rows)

    async def resolve_corporate_action_factors(self, **kwargs) -> dict:
        return {"resolved": 0}

    async def upsert_interest_rates(self, rows: list[dict]) -> int:
        return len(rows)

    async def refresh_expiry_calendar(self, trade_date: date) -> None:
        return None

    async def risk_free_rate(self, trade_date: date, default_rate: float) -> float:
        return default_rate

    async def equity_ohlc_window(self, symbol: str, trade_date: date, limit: int) -> list[dict]:
        return []


def test_pipeline_can_continue_when_corporate_action_sync_is_lenient() -> None:
    pipeline = Pipeline(
        settings=Settings(pipeline_compute_concurrency=1),
        repository=FakeRepository(),  # type: ignore[arg-type]
        bhavcopy_source=FakeBhavcopySource(),  # type: ignore[arg-type]
        rates=FakeRates(),  # type: ignore[arg-type]
        corporate_actions_source=FailingCorporateActions(),  # type: ignore[arg-type]
    )

    result = asyncio.run(
        pipeline.run_for_date(
            date(2026, 7, 7),
            finalize=False,
            strict_corporate_actions=False,
        )
    )

    assert result["options_rows"] == 1
    assert result["equity_rows"] == 1
    assert result["corporate_action_sync_status"] == "failed"
    assert result["corporate_action_sync_error"]["type"] == "RuntimeError"


def test_pipeline_loads_index_ohlc_from_yahoo_before_computing_metrics() -> None:
    trade_date = date(2026, 7, 7)

    class IndexBhavcopySource(FakeBhavcopySource):
        async def fetch_fo(self, requested_date: date) -> list[OptionBhavcopyRow]:
            return [
                OptionBhavcopyRow(
                    symbol="NIFTY",
                    trade_date=requested_date,
                    expiry_date=date(2026, 7, 30),
                    strike_price=25_000.0,
                    option_type="CE",
                    instrument_type="OPTIDX",
                    open=100.0,
                    high=120.0,
                    low=90.0,
                    close=110.0,
                    settle_price=110.0,
                    num_contracts=100,
                    contract_value=1_000.0,
                    open_interest=1_000,
                    change_in_oi=10,
                    source="unit",
                )
            ]

        async def fetch_cm(self, requested_date: date) -> list[EquityBhavcopyRow]:
            return []

    class IndexPriceSource:
        async def fetch_equity_history(self, symbol: str, start: date, end: date) -> list[EquityBar]:
            assert symbol == "NIFTY"
            assert start == trade_date
            assert end == date(2026, 7, 8)
            return [
                EquityBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=25_000.0,
                    high=25_200.0,
                    low=24_900.0,
                    close=25_100.0,
                    volume=0,
                    source="yahoo:^NSEI",
                )
            ]

    class IndexRepository(FakeRepository):
        def __init__(self) -> None:
            self.equity_rows: list[EquityBhavcopyRow | EquityBar] = []

        async def upsert_equity_rows(self, rows: list[EquityBhavcopyRow | EquityBar]) -> int:
            self.equity_rows = rows
            return len(rows)

    repo = IndexRepository()
    pipeline = Pipeline(
        settings=Settings(pipeline_compute_concurrency=1),
        repository=repo,  # type: ignore[arg-type]
        bhavcopy_source=IndexBhavcopySource(),  # type: ignore[arg-type]
        rates=FakeRates(),  # type: ignore[arg-type]
        index_price_source=IndexPriceSource(),  # type: ignore[arg-type]
    )

    result = asyncio.run(pipeline.run_for_date(trade_date, finalize=False))

    assert result["index_symbols"] == 1
    assert result["index_equity_rows"] == 1
    assert result["index_price_errors"] == []
    assert repo.equity_rows[0].symbol == "NIFTY"
    assert repo.equity_rows[0].source == "yahoo:^NSEI"
