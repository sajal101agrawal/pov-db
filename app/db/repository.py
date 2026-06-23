from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from typing import Any, Iterable

import asyncpg

from app.services.corporate_actions import derive_price_multiplier
from app.sources.models import EquityBar, EquityBhavcopyRow, OptionBhavcopyRow
from app.sources.nse_metadata import SymbolMetadata


class MarketRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def latest_trade_date(self) -> date | None:
        return await self.pool.fetchval("SELECT MAX(trade_date) FROM options_historical")

    async def active_symbols(self) -> list[str]:
        rows = await self.pool.fetch(
            """
            SELECT symbol
            FROM symbol_universe
            WHERE is_active
            ORDER BY symbol
            """
        )
        if rows:
            return [row["symbol"] for row in rows]

        rows = await self.pool.fetch(
            """
            SELECT symbol
            FROM (
                SELECT DISTINCT symbol
                FROM symbol_daily_metrics
                UNION
                SELECT DISTINCT symbol
                FROM options_historical
            ) discovered
            UNION
            SELECT symbol
            FROM symbol_universe
            WHERE is_active
            ORDER BY symbol
            """
        )
        return [row["symbol"] for row in rows]

    async def yahoo_symbols_for(self, symbols: list[str]) -> dict[str, str | None]:
        if not symbols:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT symbol, yahoo_symbol
            FROM symbol_universe
            WHERE symbol = ANY($1::text[])
            """,
            symbols,
        )
        return {row["symbol"]: row["yahoo_symbol"] for row in rows}

    async def live_baseline(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (sdm.symbol)
                   sdm.symbol,
                   sdm.trade_date,
                   sdm.avg_option_volume::float,
                   sdm.iv_30::float,
                   sdm.iv_60::float,
                   sdm.iv_90::float,
                   CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_30::float END AS rv_30,
                   CASE WHEN sdm.vrp_signal_enabled THEN sdm.vrp::float END AS vrp,
                   sdm.rv_data_status,
                   sdm.rv_calculation_version,
                   sdm.vrp_signal_enabled,
                   sdm.skew_25::float,
                   sdm.fwdv_3060::float,
                   sdm.fwdfct_3060::float,
                   sdm.fev_30::float,
                   sdm.iv_slope_3060::float,
                   sdm.dte_30,
                   sdm.dte_60,
                   sdm.dte_90,
                   sdm.expiry_30d,
                   sdm.expiry_60d,
                   sdm.expiry_90d,
                   su.company_name,
                   su.symbol_type,
                   su.sector,
                   su.industry
            FROM symbol_daily_metrics sdm
            LEFT JOIN symbol_universe su USING (symbol)
            WHERE sdm.symbol = ANY($1::varchar[])
            ORDER BY sdm.symbol, sdm.trade_date DESC
            """,
            symbols,
        )
        return {row["symbol"]: dict(row) for row in rows}

    async def upsert_discovered_symbols(self, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO symbol_universe (symbol, symbol_type, is_active, updated_at)
                VALUES ($1, $2, TRUE, NOW())
                ON CONFLICT (symbol)
                DO UPDATE SET
                    symbol_type = COALESCE(symbol_universe.symbol_type, EXCLUDED.symbol_type),
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                [(item["symbol"], item.get("symbol_type")) for item in items],
            )
        return len(items)

    async def upsert_symbol_metadata(self, rows: Iterable[SymbolMetadata]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO symbol_universe (
                    symbol, company_name, isin, sector, industry, lot_size, tick_size,
                    is_nifty50, is_nifty100, is_banknifty, is_midcap, is_active,
                    yahoo_symbol, updated_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,FALSE,$12,NOW())
                ON CONFLICT (symbol)
                DO UPDATE SET
                    company_name = COALESCE(EXCLUDED.company_name, symbol_universe.company_name),
                    isin = COALESCE(EXCLUDED.isin, symbol_universe.isin),
                    sector = COALESCE(EXCLUDED.sector, symbol_universe.sector),
                    industry = COALESCE(EXCLUDED.industry, symbol_universe.industry),
                    lot_size = COALESCE(EXCLUDED.lot_size, symbol_universe.lot_size),
                    tick_size = COALESCE(EXCLUDED.tick_size, symbol_universe.tick_size),
                    is_nifty50 = EXCLUDED.is_nifty50,
                    is_nifty100 = EXCLUDED.is_nifty100,
                    is_banknifty = EXCLUDED.is_banknifty,
                    is_midcap = EXCLUDED.is_midcap,
                    yahoo_symbol = COALESCE(symbol_universe.yahoo_symbol, EXCLUDED.yahoo_symbol),
                    updated_at = NOW()
                """,
                [
                    (
                        item.symbol,
                        item.company_name,
                        item.isin,
                        item.sector,
                        item.industry,
                        item.lot_size,
                        item.tick_size,
                        item.is_nifty50,
                        item.is_nifty100,
                        item.is_banknifty,
                        item.is_midcap,
                        f"{item.symbol}.NS",
                    )
                    for item in items
                ],
            )
        return len(items)

    async def upsert_trading_calendar(self, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO trading_calendar (trade_date, is_trading_day, source)
                VALUES ($1, $2, $3)
                ON CONFLICT (trade_date)
                DO UPDATE SET
                    is_trading_day = EXCLUDED.is_trading_day,
                    source = EXCLUDED.source
                """,
                [(item["trade_date"], item["is_trading_day"], item.get("source")) for item in items],
            )
        return len(items)

    async def log_error(
        self,
        task_name: str,
        error_type: str,
        error_details: dict[str, Any],
        *,
        symbol: str | None = None,
        trade_date: date | None = None,
        source: str | None = None,
    ) -> int:
        error_id = await self.pool.fetchval(
            """
            INSERT INTO error_log (task_name, symbol, trade_date, source, error_type, error_details)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            task_name,
            symbol,
            trade_date,
            source,
            error_type,
            json.dumps(error_details, default=str),
        )
        return int(error_id)

    async def upsert_option_rows(self, rows: Iterable[OptionBhavcopyRow]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO options_historical (
                    symbol, trade_date, expiry_date, strike_price, option_type, instrument_type,
                    open, high, low, close, settle_price, num_contracts, contract_value,
                    open_interest, change_in_oi, days_to_expiry, source
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (symbol, trade_date, expiry_date, strike_price, option_type)
                DO UPDATE SET
                    instrument_type = EXCLUDED.instrument_type,
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    settle_price = EXCLUDED.settle_price,
                    num_contracts = EXCLUDED.num_contracts,
                    contract_value = EXCLUDED.contract_value,
                    open_interest = EXCLUDED.open_interest,
                    change_in_oi = EXCLUDED.change_in_oi,
                    days_to_expiry = EXCLUDED.days_to_expiry,
                    source = EXCLUDED.source
                """,
                [
                    (
                        r.symbol,
                        r.trade_date,
                        r.expiry_date,
                        r.strike_price,
                        r.option_type,
                        r.instrument_type,
                        r.open,
                        r.high,
                        r.low,
                        r.close,
                        r.settle_price,
                        r.num_contracts,
                        r.contract_value,
                        r.open_interest,
                        r.change_in_oi,
                        max((r.expiry_date - r.trade_date).days, 0),
                        r.source,
                    )
                    for r in items
                ],
            )
        return len(items)

    async def upsert_equity_rows(self, rows: Iterable[EquityBhavcopyRow | EquityBar]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO equity_historical (
                    symbol, trade_date, open, high, low, close, volume, turnover, delivery_volume, source
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (symbol, trade_date)
                DO UPDATE SET
                    open = COALESCE(EXCLUDED.open, equity_historical.open),
                    high = COALESCE(EXCLUDED.high, equity_historical.high),
                    low = COALESCE(EXCLUDED.low, equity_historical.low),
                    close = COALESCE(EXCLUDED.close, equity_historical.close),
                    volume = COALESCE(EXCLUDED.volume, equity_historical.volume),
                    turnover = COALESCE(EXCLUDED.turnover, equity_historical.turnover),
                    delivery_volume = COALESCE(EXCLUDED.delivery_volume, equity_historical.delivery_volume),
                    source = EXCLUDED.source
                """,
                [
                    (
                        r.symbol,
                        r.trade_date,
                        r.open,
                        r.high,
                        r.low,
                        r.close,
                        getattr(r, "volume", None),
                        getattr(r, "turnover", None),
                        getattr(r, "delivery_volume", None),
                        r.source,
                    )
                    for r in items
                ],
            )
        return len(items)

    async def upsert_interest_rates(self, rows: Iterable[tuple[date, float, str]], tenor: str = "91d") -> int:
        items = list(rows)
        if not items:
            return 0
        for rate_date, rate, source in items:
            if rate < 0 or rate > 0.25:
                raise ValueError(f"Risk-free rate out of range for {rate_date}: {rate} from {source}")
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO interest_rates (rate_date, tenor, rate, source)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (rate_date, tenor)
                DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source
                """,
                [(d, tenor, rate, source) for d, rate, source in items],
            )
        return len(items)

    async def upsert_events(self, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO events (symbol, event_date, event_type, description, source)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (symbol, event_date, event_type)
                DO UPDATE SET
                    description = EXCLUDED.description,
                    source = EXCLUDED.source
                """,
                [
                    (
                        item["symbol"],
                        item["event_date"],
                        item["event_type"],
                        item.get("description"),
                        item.get("source", "nse:event-calendar"),
                    )
                    for item in items
                ],
            )
        return len(items)

    async def upsert_corporate_actions(self, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO corporate_actions (
                    symbol, ex_date, record_date, action_type, description, face_value,
                    price_multiplier, cash_amount, rights_new_shares, rights_held_shares,
                    subscription_price, adjustment_status, factor_source, source,
                    source_key, raw_payload
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb)
                ON CONFLICT (source, source_key)
                DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    ex_date = EXCLUDED.ex_date,
                    record_date = EXCLUDED.record_date,
                    action_type = EXCLUDED.action_type,
                    description = EXCLUDED.description,
                    face_value = EXCLUDED.face_value,
                    price_multiplier = CASE
                        WHEN corporate_actions.factor_source = 'MANUAL'
                            THEN corporate_actions.price_multiplier
                        ELSE EXCLUDED.price_multiplier
                    END,
                    cash_amount = EXCLUDED.cash_amount,
                    rights_new_shares = EXCLUDED.rights_new_shares,
                    rights_held_shares = EXCLUDED.rights_held_shares,
                    subscription_price = EXCLUDED.subscription_price,
                    adjustment_status = CASE
                        WHEN corporate_actions.factor_source = 'MANUAL'
                            THEN corporate_actions.adjustment_status
                        ELSE EXCLUDED.adjustment_status
                    END,
                    factor_source = CASE
                        WHEN corporate_actions.factor_source = 'MANUAL'
                            THEN corporate_actions.factor_source
                        ELSE EXCLUDED.factor_source
                    END,
                    raw_payload = EXCLUDED.raw_payload,
                    updated_at = NOW()
                """,
                [
                    (
                        item["symbol"],
                        item["ex_date"],
                        item.get("record_date"),
                        item["action_type"],
                        item["description"],
                        item.get("face_value"),
                        item.get("price_multiplier"),
                        item.get("cash_amount"),
                        item.get("rights_new_shares"),
                        item.get("rights_held_shares"),
                        item.get("subscription_price"),
                        item.get("adjustment_status", "PENDING_FACTOR"),
                        item.get("factor_source"),
                        item.get("source", "nse:corporate-actions"),
                        item["source_key"],
                        item.get("raw_payload", "{}"),
                    )
                    for item in items
                ],
            )
        return len(items)

    async def resolve_corporate_action_factors(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, int]:
        rows = await self.pool.fetch(
            """
            SELECT ca.*,
                   prior.close::float AS previous_close,
                   (
                       SELECT COUNT(*)
                       FROM corporate_actions peer
                       WHERE peer.symbol = ca.symbol
                         AND peer.ex_date = ca.ex_date
                         AND peer.adjustment_status != 'IGNORED'
                   ) AS same_date_action_count
            FROM corporate_actions ca
            LEFT JOIN LATERAL (
                SELECT close
                FROM equity_historical eh
                WHERE eh.symbol = ca.symbol
                  AND eh.trade_date < ca.ex_date
                  AND eh.close IS NOT NULL
                ORDER BY eh.trade_date DESC
                LIMIT 1
            ) prior ON TRUE
            WHERE ca.adjustment_status = 'PENDING_FACTOR'
              AND ($1::date IS NULL OR ca.ex_date >= $1)
              AND ($2::date IS NULL OR ca.ex_date <= $2)
              AND ($3::text[] IS NULL OR ca.symbol = ANY($3::text[]))
            ORDER BY ca.ex_date, ca.symbol, ca.id
            """,
            start,
            end,
            symbols,
        )
        updates: list[tuple[float, str, int]] = []
        for row in rows:
            action = dict(row)
            multiplier, factor_source = derive_price_multiplier(
                action,
                action.get("previous_close"),
                same_date_action_count=int(action.get("same_date_action_count") or 1),
            )
            if multiplier is not None and factor_source is not None:
                updates.append((multiplier, factor_source, int(action["id"])))
        if updates:
            await self.pool.executemany(
                """
                UPDATE corporate_actions
                SET price_multiplier = $1,
                    factor_source = $2,
                    adjustment_status = 'VERIFIED',
                    updated_at = NOW()
                WHERE id = $3 AND adjustment_status = 'PENDING_FACTOR'
                """,
                updates,
            )
        return {
            "pending_examined": len(rows),
            "resolved": len(updates),
            "still_pending": len(rows) - len(updates),
        }

    async def corporate_actions_window(
        self, symbol: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, symbol, ex_date, record_date, action_type, description,
                   price_multiplier::float, cash_amount::float,
                   rights_new_shares::float, rights_held_shares::float,
                   subscription_price::float, adjustment_status, factor_source, source
            FROM corporate_actions
            WHERE symbol = $1
              AND ex_date > $2
              AND ex_date <= $3
              AND adjustment_status != 'IGNORED'
            ORDER BY ex_date, id
            """,
            symbol.upper(),
            start,
            end,
        )
        return [dict(row) for row in rows]

    async def corporate_actions_for_symbol(
        self, symbol: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, symbol, ex_date, record_date, action_type, description,
                   price_multiplier::float, cash_amount::float,
                   adjustment_status, factor_source, source, updated_at, created_at
            FROM corporate_actions
            WHERE symbol = $1
            ORDER BY ex_date DESC, id DESC
            LIMIT $2
            """,
            symbol.upper(),
            limit,
        )
        return [dict(row) for row in rows]

    async def delete_future_events_by_source(
        self,
        symbols: list[str],
        source: str,
        min_event_date: date,
        event_type: str = "RESULT",
    ) -> int:
        if not symbols:
            return 0
        result = await self.pool.execute(
            """
            DELETE FROM events
            WHERE symbol = ANY($1::text[])
              AND event_type = $2
              AND source = $3
              AND event_date >= $4
            """,
            symbols,
            event_type,
            source,
            min_event_date,
        )
        return int(result.split()[-1])

    async def insert_live_snapshot(
        self,
        symbol: str,
        snapshot_time: datetime,
        payload: dict[str, Any],
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO live_snapshot (
                symbol, snapshot_time, current_price, pnl, maxloss, option_chain
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (symbol, snapshot_time)
            DO UPDATE SET
                current_price = EXCLUDED.current_price,
                pnl = EXCLUDED.pnl,
                maxloss = EXCLUDED.maxloss,
                option_chain = EXCLUDED.option_chain
            """,
            symbol.upper(),
            snapshot_time,
            payload.get("underlying_last_price"),
            payload.get("pnl"),
            payload.get("maxloss"),
            json.dumps(payload, default=str),
        )

    async def refresh_expiry_calendar(self, trade_date: date) -> int:
        result = await self.pool.execute(
            """
            INSERT INTO expiry_calendar (symbol, expiry_date, instrument_type, expiry_type)
            SELECT DISTINCT symbol, expiry_date, instrument_type,
                   CASE
                       WHEN expiry_date = MAX(expiry_date) OVER (
                            PARTITION BY symbol, date_trunc('month', expiry_date)
                       ) THEN 'monthly'
                       ELSE 'weekly'
                   END AS expiry_type
            FROM options_historical
            WHERE trade_date = $1
            ON CONFLICT (symbol, expiry_date)
            DO UPDATE SET
                instrument_type = EXCLUDED.instrument_type,
                expiry_type = EXCLUDED.expiry_type
            """,
            trade_date,
        )
        return int(result.split()[-1])

    async def risk_free_rate(self, trade_date: date, default: float) -> float:
        rate = await self.pool.fetchval(
            """
            SELECT rate::float
            FROM interest_rates
            WHERE tenor = '91d' AND rate_date <= $1
            ORDER BY rate_date DESC
            LIMIT 1
            """,
            trade_date,
        )
        return float(rate) if rate is not None else default

    async def has_event(self, symbol: str, event_date: date, event_type: str) -> bool:
        value = await self.pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM events
                WHERE symbol = $1 AND event_date = $2 AND event_type = $3
            )
            """,
            symbol,
            event_date,
            event_type,
        )
        return bool(value)

    async def lagged_iv30(self, symbol: str, trade_date: date, lag_trading_days: int = 20) -> float | None:
        if lag_trading_days < 1:
            return None
        value = await self.pool.fetchval(
            """
            SELECT iv_30::float
            FROM symbol_daily_metrics
            WHERE symbol = $1 AND trade_date < $2 AND iv_30 IS NOT NULL
            ORDER BY trade_date DESC
            OFFSET $3
            LIMIT 1
            """,
            symbol,
            trade_date,
            lag_trading_days - 1,
        )
        return float(value) if value is not None else None

    async def equity_ohlc_window(self, symbol: str, trade_date: date, limit: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT trade_date, open::float, high::float, low::float, close::float
            FROM equity_historical
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
            """,
            symbol,
            trade_date,
            limit,
        )
        return [dict(row) for row in reversed(rows)]

    async def option_chain(self, symbol: str, trade_date: date, expiry_date: date | None = None) -> list[dict[str, Any]]:
        if expiry_date:
            rows = await self.pool.fetch(
                """
                SELECT symbol, trade_date, expiry_date, strike_price::float, option_type,
                       open::float, close::float, settle_price::float, iv::float, delta::float,
                       num_contracts
                FROM options_historical
                WHERE symbol = $1 AND trade_date = $2 AND expiry_date = $3
                """,
                symbol,
                trade_date,
                expiry_date,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT symbol, trade_date, expiry_date, strike_price::float, option_type,
                       open::float, close::float, settle_price::float, iv::float, delta::float,
                       num_contracts
                FROM options_historical
                WHERE symbol = $1 AND trade_date = $2
                """,
                symbol,
                trade_date,
            )
        return [dict(row) for row in rows]

    async def update_contract_derived(self, records: Iterable[dict[str, Any]]) -> int:
        items = list(records)
        if not items:
            return 0
        payload = [
            (
                r["symbol"],
                r["trade_date"],
                r["expiry_date"],
                r["strike_price"],
                r["option_type"],
                r.get("iv"),
                r.get("delta"),
                r.get("gamma"),
                r.get("theta"),
                r.get("vega"),
                r.get("rho"),
                r.get("is_atm"),
            )
            for r in items
        ]
        symbol_key = payload[0][0]
        trade_date_key = payload[0][1]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    CREATE TEMP TABLE tmp_option_derived (
                        symbol TEXT,
                        trade_date DATE,
                        expiry_date DATE,
                        strike_price NUMERIC,
                        option_type TEXT,
                        iv DOUBLE PRECISION,
                        delta DOUBLE PRECISION,
                        gamma DOUBLE PRECISION,
                        theta DOUBLE PRECISION,
                        vega DOUBLE PRECISION,
                        rho DOUBLE PRECISION,
                        is_atm BOOLEAN
                    ) ON COMMIT DROP
                    """
                )
                await conn.copy_records_to_table(
                    "tmp_option_derived",
                    records=payload,
                    columns=[
                        "symbol",
                        "trade_date",
                        "expiry_date",
                        "strike_price",
                        "option_type",
                        "iv",
                        "delta",
                        "gamma",
                        "theta",
                        "vega",
                        "rho",
                        "is_atm",
                    ],
                )
                await conn.execute(
                    """
                    UPDATE options_historical AS target
                    SET iv = source.iv,
                        delta = source.delta,
                        gamma = source.gamma,
                        theta = source.theta,
                        vega = source.vega,
                        rho = source.rho,
                        is_atm = source.is_atm
                    FROM tmp_option_derived AS source
                    WHERE target.symbol = $1
                      AND target.trade_date = $2
                      AND target.symbol = source.symbol
                      AND target.trade_date = source.trade_date
                      AND target.expiry_date = source.expiry_date
                      AND target.strike_price = source.strike_price
                      AND target.option_type = source.option_type
                    """,
                    symbol_key,
                    trade_date_key,
                )
        return len(items)

    async def upsert_daily_metric(self, metric: dict[str, Any]) -> None:
        columns = list(metric.keys())
        values = [metric[col] for col in columns]
        assignments = ", ".join(f"{col}=EXCLUDED.{col}" for col in columns if col not in {"symbol", "trade_date"})
        placeholders = ", ".join(f"${idx}" for idx in range(1, len(values) + 1))
        await self.pool.execute(
            f"""
            INSERT INTO symbol_daily_metrics ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (symbol, trade_date)
            DO UPDATE SET {assignments}, updated_at = NOW()
            """,
            *values,
        )

    async def upsert_straddle_pnl(self, row: dict[str, Any]) -> None:
        columns = list(row.keys())
        values = [row[col] for col in columns]
        assignments = ", ".join(f"{col}=EXCLUDED.{col}" for col in columns if col not in {"symbol", "trade_date"})
        placeholders = ", ".join(f"${idx}" for idx in range(1, len(values) + 1))
        await self.pool.execute(
            f"""
            INSERT INTO straddle_pnl ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (symbol, trade_date)
            DO UPDATE SET {assignments}
            """,
            *values,
        )

    async def refresh_percentiles(self, trade_date: date) -> None:
        await self.pool.execute(
            """
            WITH hist AS (
                SELECT cur.symbol,
                       CASE WHEN cur.iv_30 IS NULL THEN NULL ELSE
                           100.0 * percent_rank(cur.iv_30) WITHIN GROUP (ORDER BY h.iv_30)
                           FILTER (WHERE h.iv_30 IS NOT NULL)
                       END AS iv30_pct,
                       CASE WHEN cur.iv_60 IS NULL THEN NULL ELSE
                           100.0 * percent_rank(cur.iv_60) WITHIN GROUP (ORDER BY h.iv_60)
                           FILTER (WHERE h.iv_60 IS NOT NULL)
                       END AS iv60_pct,
                       CASE WHEN cur.iv_90 IS NULL THEN NULL ELSE
                           100.0 * percent_rank(cur.iv_90) WITHIN GROUP (ORDER BY h.iv_90)
                           FILTER (WHERE h.iv_90 IS NOT NULL)
                       END AS iv90_pct,
                       CASE
                           WHEN cur.vrp IS NULL
                             OR BOOL_OR(h.rv_calculation_version < 2)
                           THEN NULL
                           ELSE
                           100.0 * percent_rank(cur.vrp) WITHIN GROUP (ORDER BY h.vrp)
                           FILTER (WHERE h.vrp IS NOT NULL AND h.vrp_signal_enabled)
                       END AS vrp_pct
                FROM symbol_daily_metrics cur
                JOIN LATERAL (
                    SELECT iv_30, iv_60, iv_90, vrp,
                           vrp_signal_enabled, rv_calculation_version
                    FROM symbol_daily_metrics h
                    WHERE h.symbol = cur.symbol
                      AND h.trade_date <= cur.trade_date
                    ORDER BY h.trade_date DESC
                    LIMIT 252
                ) h ON TRUE
                WHERE cur.trade_date = $1
                GROUP BY cur.symbol, cur.iv_30, cur.iv_60, cur.iv_90, cur.vrp
            ),
            ranked AS (
                SELECT symbol,
                       RANK() OVER (ORDER BY skew_25 DESC NULLS LAST) AS skew_rank,
                       CUME_DIST() OVER (ORDER BY skew_25) * 100.0 AS skew_percentile
                FROM symbol_daily_metrics
                WHERE trade_date = $1
            )
            UPDATE symbol_daily_metrics sdm
            SET iv_30_percentile = hist.iv30_pct,
                iv_60_percentile = hist.iv60_pct,
                iv_90_percentile = hist.iv90_pct,
                vrp_percentile = hist.vrp_pct,
                skew_percentile = ranked.skew_percentile,
                skew_rank = ranked.skew_rank,
                updated_at = NOW()
            FROM hist
            JOIN ranked USING (symbol)
            WHERE sdm.symbol = hist.symbol AND sdm.trade_date = $1
            """,
            trade_date,
        )

    async def refresh_aggregates(self) -> None:
        await self.pool.execute(
            """
            WITH straddle_daily AS (
                SELECT
                    sp.symbol,
                    ROUND(100.0 * COUNT(*) FILTER (WHERE sp.is_winner) / NULLIF(COUNT(*), 0), 2) AS win_rate,
                    AVG(sp.pnl) AS avg_straddle_pnl,
                    AVG(sp.pnl / NULLIF(sp.total_entry, 0)) AS avg_straddle_pnl_pct,
                    AVG(sp.call_entry - sp.call_exit) AS avg_call_pnl,
                    AVG(sp.put_entry - sp.put_exit) AS avg_put_pnl,
                    MAX(sp.pnl) AS max_profit,
                    MIN(sp.pnl) AS max_loss
                FROM straddle_pnl sp
                WHERE sp.skip_reason IS NULL
                GROUP BY sp.symbol
            ),
            metric_daily AS (
                SELECT
                    symbol,
                    ROUND(
                        100.0 * COUNT(*) FILTER (WHERE vrp_signal_enabled AND vrp > 0)
                        / NULLIF(COUNT(vrp) FILTER (WHERE vrp_signal_enabled), 0),
                        2
                    ) AS vrp_win_rate,
                    AVG(vrp) FILTER (WHERE vrp_signal_enabled) AS avg_vrp_4y,
                    MIN(rv_calculation_version) AS vrp_calculation_version
                FROM symbol_daily_metrics
                GROUP BY symbol
            ),
            result_windows AS (
                SELECT ev.symbol,
                       ev.event_date,
                       entry_day.trade_date AS entry_date,
                       exit_day.trade_date AS exit_date
                FROM events ev
                JOIN LATERAL (
                    SELECT trade_date
                    FROM equity_historical eh
                    WHERE eh.symbol = ev.symbol
                      AND eh.trade_date < ev.event_date
                    ORDER BY trade_date DESC
                    LIMIT 1
                ) entry_day ON TRUE
                JOIN LATERAL (
                    SELECT trade_date
                    FROM equity_historical eh
                    WHERE eh.symbol = ev.symbol
                      AND eh.trade_date > ev.event_date
                    ORDER BY trade_date
                    LIMIT 1
                ) exit_day ON TRUE
                WHERE ev.event_type = 'RESULT'
            ),
            result_legs AS (
                SELECT rw.symbol,
                       rw.event_date,
                       sp_entry.underlying_close::float AS entry_underlying_close,
                       exit_eq.close::float AS exit_underlying_close,
                       entry_sdm.iv_30::float AS entry_iv30,
                       exit_sdm.iv_30::float AS exit_iv30,
                       sp_entry.total_exit::float AS entry_total,
                       (ce_exit.close::float + pe_exit.close::float) AS exit_total
                FROM result_windows rw
                JOIN straddle_pnl sp_entry
                  ON sp_entry.symbol = rw.symbol
                 AND sp_entry.trade_date = rw.entry_date
                 AND sp_entry.skip_reason IS NULL
                JOIN equity_historical exit_eq
                  ON exit_eq.symbol = rw.symbol AND exit_eq.trade_date = rw.exit_date
                JOIN symbol_daily_metrics entry_sdm
                  ON entry_sdm.symbol = rw.symbol AND entry_sdm.trade_date = rw.entry_date
                JOIN symbol_daily_metrics exit_sdm
                  ON exit_sdm.symbol = rw.symbol AND exit_sdm.trade_date = rw.exit_date
                JOIN options_historical ce_exit
                  ON ce_exit.symbol = rw.symbol
                 AND ce_exit.trade_date = rw.exit_date
                 AND ce_exit.expiry_date = sp_entry.expiry_date
                 AND ce_exit.strike_price = sp_entry.atm_strike
                 AND ce_exit.option_type = 'CE'
                JOIN options_historical pe_exit
                  ON pe_exit.symbol = rw.symbol
                 AND pe_exit.trade_date = rw.exit_date
                 AND pe_exit.expiry_date = sp_entry.expiry_date
                 AND pe_exit.strike_price = sp_entry.atm_strike
                 AND pe_exit.option_type = 'PE'
                WHERE sp_entry.underlying_close > 0
                  AND sp_entry.total_exit IS NOT NULL
                  AND ce_exit.close IS NOT NULL
                  AND pe_exit.close IS NOT NULL
            ),
            earnings AS (
                SELECT symbol,
                       AVG((entry_iv30 - exit_iv30) / NULLIF(entry_iv30, 0)) AS historical_iv_crush,
                       AVG(entry_total / NULLIF(entry_underlying_close, 0)) AS implied_result_move,
                       AVG(ABS(exit_underlying_close - entry_underlying_close) / NULLIF(entry_underlying_close, 0)) AS avg_result_move,
                       MAX(ABS(exit_underlying_close - entry_underlying_close) / NULLIF(entry_underlying_close, 0)) AS max_result_move,
                       AVG(entry_total - exit_total) AS avg_earnings_pnl,
                       ROUND(
                           100.0 * COUNT(*) FILTER (WHERE entry_total - exit_total > 0)
                           / NULLIF(COUNT(*), 0),
                           2
                       ) AS earnings_win_rate,
                       MAX(entry_total - exit_total) AS max_earnings_profit,
                       MIN(entry_total - exit_total) AS max_earnings_loss
                FROM result_legs
                GROUP BY symbol
            )
            INSERT INTO symbol_aggregates (
                symbol, win_rate, vrp_win_rate, avg_vrp_4y, vrp_calculation_version,
                avg_straddle_pnl,
                avg_straddle_pnl_pct, avg_call_pnl, avg_put_pnl, max_profit, max_loss,
                historical_iv_crush, implied_result_move, avg_result_move, max_result_move,
                avg_earnings_pnl, earnings_win_rate, max_earnings_profit, max_earnings_loss,
                updated_at
            )
            SELECT
                straddle_daily.symbol,
                straddle_daily.win_rate,
                metric_daily.vrp_win_rate,
                metric_daily.avg_vrp_4y,
                metric_daily.vrp_calculation_version,
                straddle_daily.avg_straddle_pnl,
                straddle_daily.avg_straddle_pnl_pct,
                straddle_daily.avg_call_pnl,
                straddle_daily.avg_put_pnl,
                straddle_daily.max_profit,
                straddle_daily.max_loss,
                earnings.historical_iv_crush,
                earnings.implied_result_move,
                earnings.avg_result_move,
                earnings.max_result_move,
                earnings.avg_earnings_pnl,
                earnings.earnings_win_rate,
                earnings.max_earnings_profit,
                earnings.max_earnings_loss,
                NOW()
            FROM straddle_daily
            LEFT JOIN metric_daily USING (symbol)
            LEFT JOIN earnings USING (symbol)
            ON CONFLICT (symbol) DO UPDATE SET
                win_rate = EXCLUDED.win_rate,
                vrp_win_rate = EXCLUDED.vrp_win_rate,
                avg_vrp_4y = EXCLUDED.avg_vrp_4y,
                vrp_calculation_version = EXCLUDED.vrp_calculation_version,
                avg_straddle_pnl = EXCLUDED.avg_straddle_pnl,
                avg_straddle_pnl_pct = EXCLUDED.avg_straddle_pnl_pct,
                avg_call_pnl = EXCLUDED.avg_call_pnl,
                avg_put_pnl = EXCLUDED.avg_put_pnl,
                max_profit = EXCLUDED.max_profit,
                max_loss = EXCLUDED.max_loss,
                historical_iv_crush = EXCLUDED.historical_iv_crush,
                implied_result_move = EXCLUDED.implied_result_move,
                avg_result_move = EXCLUDED.avg_result_move,
                max_result_move = EXCLUDED.max_result_move,
                avg_earnings_pnl = EXCLUDED.avg_earnings_pnl,
                earnings_win_rate = EXCLUDED.earnings_win_rate,
                max_earnings_profit = EXCLUDED.max_earnings_profit,
                max_earnings_loss = EXCLUDED.max_earnings_loss,
                updated_at = NOW()
            """
        )

    async def dashboard_row(self, symbol: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT to_jsonb(sdm.*) || COALESCE(to_jsonb(sa.*), '{}'::jsonb) ||
                   COALESCE(jsonb_build_object(
                       'company_name', su.company_name,
                       'sector', su.sector,
                       'industry', su.industry,
                       'is_nifty50', su.is_nifty50,
                       'is_nifty100', su.is_nifty100,
                       'symbol_type', su.symbol_type
                   ), '{}'::jsonb) ||
                   jsonb_build_object(
                       'rv_10', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_10 END,
                       'rv_20', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_20 END,
                       'rv_30', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_30 END,
                       'rv_60', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_60 END,
                       'rv_90', CASE WHEN sdm.rv_calculation_version >= 2 THEN sdm.rv_90 END,
                       'vrp', CASE WHEN sdm.vrp_signal_enabled THEN sdm.vrp END,
                       'avg_vrp_4y', CASE
                           WHEN sa.vrp_calculation_version >= 2 THEN sa.avg_vrp_4y
                       END,
                       'vrp_win_rate', CASE
                           WHEN sa.vrp_calculation_version >= 2 THEN sa.vrp_win_rate
                       END
                   ) AS payload
            FROM symbol_daily_metrics sdm
            LEFT JOIN symbol_aggregates sa USING (symbol)
            LEFT JOIN symbol_universe su USING (symbol)
            WHERE sdm.symbol = $1
            ORDER BY sdm.trade_date DESC
            LIMIT 1
            """,
            symbol.upper(),
        )
        return json.loads(row["payload"]) if row else None

    async def history(self, symbol: str, days: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT trade_date,
                   -- Implied volatility term structure
                   iv_30::float, iv_60::float, iv_90::float,
                   -- Realized volatility (all windows)
                   CASE WHEN rv_calculation_version >= 2 THEN rv_10::float END AS rv_10,
                   CASE WHEN rv_calculation_version >= 2 THEN rv_20::float END AS rv_20,
                   CASE WHEN rv_calculation_version >= 2 THEN rv_30::float END AS rv_30,
                   CASE WHEN rv_calculation_version >= 2 THEN rv_60::float END AS rv_60,
                   CASE WHEN rv_calculation_version >= 2 THEN rv_90::float END AS rv_90,
                   rv_10_raw::float, rv_20_raw::float, rv_30_raw::float,
                   rv_60_raw::float, rv_90_raw::float,
                   rv_data_status, rv_adjustment_details, rv_calculation_version,
                   -- Variance risk premium
                   CASE WHEN vrp_signal_enabled THEN vrp::float END AS vrp,
                   vrp_signal_enabled,
                   -- Forward volatility
                   fwdv_3060::float, fev_30::float,
                   -- Skew (all delta levels)
                   skew_20::float, skew_25::float, skew_30::float, smoothed_skew::float,
                   -- IV/RV and IV/FEV ratios
                   iv30_rv30_ratio::float, iv30_fev30_ratio::float,
                   -- RSI
                   daily_rsi::float, weekly_rsi::float,
                   -- Percentiles / ranks
                   iv_30_percentile::float, iv_60_percentile::float, iv_90_percentile::float,
                   vrp_percentile::float, skew_percentile::float, skew_rank,
                   -- Volume
                   avg_option_volume::float
            FROM symbol_daily_metrics
            WHERE symbol = $1
            ORDER BY trade_date DESC
            LIMIT $2
            """,
            symbol.upper(),
            days,
        )
        return [dict(row) for row in reversed(rows)]

    async def straddle_history(self, symbol: str, days: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT trade_date,
                   expiry_date,
                   atm_strike::float,
                   underlying_open::float,
                   underlying_close::float,
                   underlying_move_pct::float,
                   call_entry::float,
                   put_entry::float,
                   total_entry::float,
                   call_exit::float,
                   put_exit::float,
                   total_exit::float,
                   pnl::float,
                   is_winner,
                   has_result_event,
                   iv_on_entry::float,
                   skip_reason
            FROM straddle_pnl
            WHERE symbol = $1
            ORDER BY trade_date DESC
            LIMIT $2
            """,
            symbol.upper(),
            days,
        )
        return [dict(row) for row in reversed(rows)]
