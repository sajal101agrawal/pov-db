from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool


async def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute daily short ATM straddle PnL from loaded EOD data.")
    parser.add_argument("--start", help="Optional start trade date YYYY-MM-DD.")
    parser.add_argument("--end", help="Optional end trade date YYYY-MM-DD.")
    parser.add_argument("--symbols", help="Optional comma-separated symbols.")
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    symbols = [item.strip().upper() for item in args.symbols.split(",")] if args.symbols else None

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO straddle_pnl (
                    symbol, trade_date, expiry_date, atm_strike,
                    underlying_open, underlying_close, underlying_move_pct,
                    call_entry, put_entry, total_entry,
                    call_exit, put_exit, total_exit,
                    pnl, is_winner, has_result_event, iv_on_entry, skip_reason
                )
                SELECT
                    sdm.symbol,
                    sdm.trade_date,
                    NULL,
                    NULL,
                    eh.open,
                    eh.close,
                    CASE WHEN eh.open > 0 THEN (eh.close - eh.open) / eh.open * 100.0 ELSE NULL END,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    EXISTS (
                        SELECT 1
                        FROM events ev
                        WHERE ev.symbol = sdm.symbol
                          AND ev.event_date = sdm.trade_date
                          AND ev.event_type = 'RESULT'
                    ),
                    sdm.iv_30,
                    CASE WHEN sdm.expiry_30d IS NULL THEN 'NO_EXPIRY' ELSE 'MISSING_LEGS' END
                FROM symbol_daily_metrics sdm
                LEFT JOIN equity_historical eh USING (symbol, trade_date)
                WHERE ($1::date IS NULL OR sdm.trade_date >= $1)
                  AND ($2::date IS NULL OR sdm.trade_date <= $2)
                  AND ($3::varchar[] IS NULL OR sdm.symbol = ANY($3))
                ON CONFLICT (symbol, trade_date) DO UPDATE SET
                    expiry_date = EXCLUDED.expiry_date,
                    atm_strike = EXCLUDED.atm_strike,
                    underlying_open = EXCLUDED.underlying_open,
                    underlying_close = EXCLUDED.underlying_close,
                    underlying_move_pct = EXCLUDED.underlying_move_pct,
                    call_entry = EXCLUDED.call_entry,
                    put_entry = EXCLUDED.put_entry,
                    total_entry = EXCLUDED.total_entry,
                    call_exit = EXCLUDED.call_exit,
                    put_exit = EXCLUDED.put_exit,
                    total_exit = EXCLUDED.total_exit,
                    pnl = EXCLUDED.pnl,
                    is_winner = EXCLUDED.is_winner,
                    has_result_event = EXCLUDED.has_result_event,
                    iv_on_entry = EXCLUDED.iv_on_entry,
                    skip_reason = EXCLUDED.skip_reason
                """,
                start,
                end,
                symbols,
            )
            result = await conn.execute(
                """
                WITH strike_candidates AS (
                    SELECT DISTINCT
                        sdm.symbol,
                        sdm.trade_date,
                        sdm.expiry_30d AS expiry_date,
                        eh.open AS underlying_open,
                        eh.close AS underlying_close,
                        sdm.iv_30,
                        oh.strike_price
                    FROM symbol_daily_metrics sdm
                    JOIN equity_historical eh USING (symbol, trade_date)
                    JOIN options_historical oh
                      ON oh.symbol = sdm.symbol
                     AND oh.trade_date = sdm.trade_date
                     AND oh.expiry_date = sdm.expiry_30d
                    WHERE sdm.expiry_30d IS NOT NULL
                      AND eh.open IS NOT NULL
                      AND eh.close IS NOT NULL
                      AND ($1::date IS NULL OR sdm.trade_date >= $1)
                      AND ($2::date IS NULL OR sdm.trade_date <= $2)
                      AND ($3::varchar[] IS NULL OR sdm.symbol = ANY($3))
                ),
                atm AS (
                    SELECT *
                    FROM (
                        SELECT
                            strike_candidates.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY symbol, trade_date
                                ORDER BY ABS(strike_price - underlying_open), strike_price
                            ) AS rn
                        FROM strike_candidates
                    ) ranked
                    WHERE rn = 1
                ),
                valid AS (
                    SELECT
                        atm.symbol,
                        atm.trade_date,
                        atm.expiry_date,
                        atm.strike_price AS atm_strike,
                        atm.underlying_open,
                        atm.underlying_close,
                        (atm.underlying_close - atm.underlying_open) / NULLIF(atm.underlying_open, 0) * 100.0 AS underlying_move_pct,
                        ce.open AS call_entry,
                        pe.open AS put_entry,
                        ce.open + pe.open AS total_entry,
                        ce.close AS call_exit,
                        pe.close AS put_exit,
                        ce.close + pe.close AS total_exit,
                        (ce.open + pe.open) - (ce.close + pe.close) AS pnl,
                        EXISTS (
                            SELECT 1
                            FROM events ev
                            WHERE ev.symbol = atm.symbol
                              AND ev.event_date = atm.trade_date
                              AND ev.event_type = 'RESULT'
                        ) AS has_result_event,
                        atm.iv_30
                    FROM atm
                    JOIN options_historical ce
                      ON ce.symbol = atm.symbol
                     AND ce.trade_date = atm.trade_date
                     AND ce.expiry_date = atm.expiry_date
                     AND ce.strike_price = atm.strike_price
                     AND ce.option_type = 'CE'
                    JOIN options_historical pe
                      ON pe.symbol = atm.symbol
                     AND pe.trade_date = atm.trade_date
                     AND pe.expiry_date = atm.expiry_date
                     AND pe.strike_price = atm.strike_price
                     AND pe.option_type = 'PE'
                    WHERE ce.open IS NOT NULL
                      AND pe.open IS NOT NULL
                      AND ce.close IS NOT NULL
                      AND pe.close IS NOT NULL
                )
                INSERT INTO straddle_pnl (
                    symbol, trade_date, expiry_date, atm_strike,
                    underlying_open, underlying_close, underlying_move_pct,
                    call_entry, put_entry, total_entry,
                    call_exit, put_exit, total_exit,
                    pnl, is_winner, has_result_event, iv_on_entry, skip_reason
                )
                SELECT
                    symbol,
                    trade_date,
                    expiry_date,
                    atm_strike,
                    underlying_open,
                    underlying_close,
                    underlying_move_pct,
                    call_entry,
                    put_entry,
                    total_entry,
                    call_exit,
                    put_exit,
                    total_exit,
                    pnl,
                    pnl > 0,
                    has_result_event,
                    iv_30,
                    NULL
                FROM valid
                ON CONFLICT (symbol, trade_date) DO UPDATE SET
                    expiry_date = EXCLUDED.expiry_date,
                    atm_strike = EXCLUDED.atm_strike,
                    underlying_open = EXCLUDED.underlying_open,
                    underlying_close = EXCLUDED.underlying_close,
                    underlying_move_pct = EXCLUDED.underlying_move_pct,
                    call_entry = EXCLUDED.call_entry,
                    put_entry = EXCLUDED.put_entry,
                    total_entry = EXCLUDED.total_entry,
                    call_exit = EXCLUDED.call_exit,
                    put_exit = EXCLUDED.put_exit,
                    total_exit = EXCLUDED.total_exit,
                    pnl = EXCLUDED.pnl,
                    is_winner = EXCLUDED.is_winner,
                    has_result_event = EXCLUDED.has_result_event,
                    iv_on_entry = EXCLUDED.iv_on_entry,
                    skip_reason = EXCLUDED.skip_reason
                """,
                start,
                end,
                symbols,
            )
        print(result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
