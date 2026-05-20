from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable

import asyncpg

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
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE,$12,NOW())
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

    async def lagged_rv30(self, symbol: str, trade_date: date, lag_trading_days: int = 20) -> float | None:
        if lag_trading_days < 1:
            return None
        value = await self.pool.fetchval(
            """
            SELECT rv_30::float
            FROM symbol_daily_metrics
            WHERE symbol = $1 AND trade_date < $2 AND rv_30 IS NOT NULL
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
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                UPDATE options_historical
                SET iv=$6, delta=$7, gamma=$8, theta=$9, vega=$10, rho=$11, is_atm=$12
                WHERE symbol=$1 AND trade_date=$2 AND expiry_date=$3 AND strike_price=$4 AND option_type=$5
                """,
                [
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
                ],
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
                       100.0 * percent_rank(cur.iv_30) WITHIN GROUP (ORDER BY h.iv_30) AS iv30_pct,
                       100.0 * percent_rank(cur.iv_60) WITHIN GROUP (ORDER BY h.iv_60) AS iv60_pct,
                       100.0 * percent_rank(cur.iv_90) WITHIN GROUP (ORDER BY h.iv_90) AS iv90_pct,
                       100.0 * percent_rank(cur.vrp) WITHIN GROUP (ORDER BY h.vrp) AS vrp_pct
                FROM symbol_daily_metrics cur
                JOIN LATERAL (
                    SELECT iv_30, iv_60, iv_90, vrp
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
            INSERT INTO symbol_aggregates (
                symbol, win_rate, vrp_win_rate, avg_vrp_4y, avg_straddle_pnl,
                avg_call_pnl, avg_put_pnl, max_profit, max_loss, historical_iv_crush,
                implied_result_move, avg_result_move, max_result_move, updated_at
            )
            SELECT
                sp.symbol,
                ROUND(100.0 * COUNT(*) FILTER (WHERE sp.is_winner) / NULLIF(COUNT(*), 0), 2),
                ROUND(100.0 * COUNT(*) FILTER (WHERE sdm.iv_30 > sdm.rv_30) / NULLIF(COUNT(*), 0), 2),
                AVG(sdm.vrp),
                AVG(sp.pnl),
                AVG(sp.call_entry - sp.call_exit),
                AVG(sp.put_entry - sp.put_exit),
                MAX(sp.pnl),
                MIN(sp.pnl),
                AVG((prev.iv_30 - next_day.iv_30) / NULLIF(prev.iv_30, 0)) FILTER (WHERE ev.event_type = 'RESULT'),
                AVG(sp.total_entry / NULLIF(sp.underlying_open, 0)) FILTER (WHERE ev.event_type = 'RESULT'),
                AVG(abs(sp.underlying_move_pct) / 100.0) FILTER (WHERE ev.event_type = 'RESULT'),
                MAX(abs(sp.underlying_move_pct) / 100.0) FILTER (WHERE ev.event_type = 'RESULT'),
                NOW()
            FROM straddle_pnl sp
            JOIN symbol_daily_metrics sdm USING (symbol, trade_date)
            LEFT JOIN events ev ON ev.symbol = sp.symbol AND ev.event_date = sp.trade_date AND ev.event_type = 'RESULT'
            LEFT JOIN symbol_daily_metrics prev ON prev.symbol = sp.symbol AND prev.trade_date = sp.trade_date
            LEFT JOIN LATERAL (
                SELECT iv_30 FROM symbol_daily_metrics n
                WHERE n.symbol = sp.symbol AND n.trade_date > sp.trade_date
                ORDER BY n.trade_date
                LIMIT 1
            ) next_day ON TRUE
            WHERE sp.skip_reason IS NULL
            GROUP BY sp.symbol
            ON CONFLICT (symbol) DO UPDATE SET
                win_rate = EXCLUDED.win_rate,
                vrp_win_rate = EXCLUDED.vrp_win_rate,
                avg_vrp_4y = EXCLUDED.avg_vrp_4y,
                avg_straddle_pnl = EXCLUDED.avg_straddle_pnl,
                avg_call_pnl = EXCLUDED.avg_call_pnl,
                avg_put_pnl = EXCLUDED.avg_put_pnl,
                max_profit = EXCLUDED.max_profit,
                max_loss = EXCLUDED.max_loss,
                historical_iv_crush = EXCLUDED.historical_iv_crush,
                implied_result_move = EXCLUDED.implied_result_move,
                avg_result_move = EXCLUDED.avg_result_move,
                max_result_move = EXCLUDED.max_result_move,
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
                   ), '{}'::jsonb) AS payload
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
            SELECT trade_date, iv_30::float, iv_60::float, iv_90::float, rv_30::float,
                   vrp::float, skew_25::float, fwdv_3060::float
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
