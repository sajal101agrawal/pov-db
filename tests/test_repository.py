from __future__ import annotations

import asyncio
from datetime import date

from app.db.repository import MarketRepository


def test_contract_derived_update_forces_custom_plan_before_timescale_dml() -> None:
    statements: list[str] = []

    class Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> None:
            return None

    class Connection:
        def transaction(self) -> Transaction:
            return Transaction()

        async def execute(self, query: str, *args: object) -> str:
            statements.append(" ".join(query.split()))
            return "UPDATE 1"

        async def copy_records_to_table(self, *args: object, **kwargs: object) -> None:
            return None

    class Acquire:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *args: object) -> None:
            return None

    class Pool:
        def acquire(self) -> Acquire:
            return Acquire()

    updated = asyncio.run(
        MarketRepository(Pool()).update_contract_derived(  # type: ignore[arg-type]
            [
                {
                    "symbol": "RELIANCE",
                    "trade_date": date(2026, 7, 21),
                    "expiry_date": date(2026, 7, 28),
                    "strike_price": 1300,
                    "option_type": "CE",
                }
            ]
        )
    )

    assert updated == 1
    assert statements[-2] == "SET LOCAL plan_cache_mode = force_custom_plan"
    assert statements[-1].startswith("UPDATE options_historical AS target")


def test_latest_live_metrics_without_symbol_filter_only_returns_active_universe() -> None:
    queries: list[str] = []

    class Pool:
        async def fetch(self, query: str, *args: object) -> list[dict]:
            queries.append(" ".join(query.split()))
            return []

    result = asyncio.run(
        MarketRepository(Pool()).latest_live_metrics()  # type: ignore[arg-type]
    )

    assert result == {}
    assert "JOIN symbol_universe su ON su.symbol = lsm.symbol" in queries[0]
    assert "WHERE su.is_active" in queries[0]
