from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool
from app.db.repository import MarketRepository


RANGE_CHECKS = {
    "interest_rates.rate_range": "SELECT COUNT(*) FROM interest_rates WHERE rate < 0 OR rate > 0.25",
    "interest_rates.non_nse_source": "SELECT COUNT(*) FROM interest_rates WHERE source IN ('yahoo:^IRX', 'fixed:india_91d')",
    "equity_historical.price_range": "SELECT COUNT(*) FROM equity_historical WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR low > high OR open < low OR open > high OR close < low OR close > high",
    "equity_historical.volume_range": "SELECT COUNT(*) FROM equity_historical WHERE volume < 0 OR turnover < 0 OR delivery_volume < 0",
    "options_historical.price_range": "SELECT COUNT(*) FROM options_historical WHERE open < 0 OR high < 0 OR low < 0 OR close < 0 OR settle_price < 0 OR (num_contracts > 0 AND (low > high OR open < low OR open > high OR close < low OR close > high))",
    "options_historical.oi_volume_range": "SELECT COUNT(*) FROM options_historical WHERE num_contracts < 0 OR open_interest < 0",
    "options_historical.dte_mismatch": "SELECT COUNT(*) FROM options_historical WHERE days_to_expiry IS DISTINCT FROM GREATEST(expiry_date - trade_date, 0)",
    "options_historical.iv_range": "SELECT COUNT(*) FROM options_historical WHERE iv IS NOT NULL AND (iv <= 0 OR iv > 5)",
    "options_historical.delta_range": "SELECT COUNT(*) FROM options_historical WHERE delta IS NOT NULL AND ((option_type = 'CE' AND (delta < 0 OR delta > 1)) OR (option_type = 'PE' AND (delta < -1 OR delta > 0)))",
    "options_historical.greeks_range": "SELECT COUNT(*) FROM options_historical WHERE (gamma IS NOT NULL AND gamma < 0) OR (vega IS NOT NULL AND vega < 0)",
    "symbol_daily_metrics.vol_range": "SELECT COUNT(*) FROM symbol_daily_metrics WHERE iv_30 < 0 OR iv_30 > 5 OR iv_60 < 0 OR iv_60 > 5 OR iv_90 < 0 OR iv_90 > 5 OR rv_10 < 0 OR rv_10 > 5 OR rv_20 < 0 OR rv_20 > 5 OR rv_30 < 0 OR rv_30 > 5 OR rv_60 < 0 OR rv_60 > 5 OR rv_90 < 0 OR rv_90 > 5",
    "symbol_daily_metrics.rsi_range": "SELECT COUNT(*) FROM symbol_daily_metrics WHERE daily_rsi < 0 OR daily_rsi > 100 OR weekly_rsi < 0 OR weekly_rsi > 100",
    "symbol_daily_metrics.percentile_range": "SELECT COUNT(*) FROM symbol_daily_metrics WHERE iv_30_percentile < 0 OR iv_30_percentile > 100 OR iv_60_percentile < 0 OR iv_60_percentile > 100 OR iv_90_percentile < 0 OR iv_90_percentile > 100 OR vrp_percentile < 0 OR vrp_percentile > 100 OR skew_percentile < 0 OR skew_percentile > 100",
    "symbol_daily_metrics.dte_matches_expiry": "SELECT COUNT(*) FROM symbol_daily_metrics WHERE dte_30 IS DISTINCT FROM (expiry_30d - trade_date) OR dte_60 IS DISTINCT FROM (expiry_60d - trade_date) OR dte_90 IS DISTINCT FROM (expiry_90d - trade_date)",
    "symbol_daily_metrics.expiry_bucket_order": "WITH monthly AS (SELECT symbol, trade_date, expiry_date, ROW_NUMBER() OVER (PARTITION BY symbol, trade_date ORDER BY expiry_date) AS rn FROM (SELECT symbol, trade_date, MAX(expiry_date) AS expiry_date FROM options_historical WHERE expiry_date >= trade_date GROUP BY symbol, trade_date, date_trunc('month', expiry_date)) x), expected AS (SELECT symbol, trade_date, MAX(expiry_date) FILTER (WHERE rn = 1) AS exp1, MAX(expiry_date) FILTER (WHERE rn = 2) AS exp2, MAX(expiry_date) FILTER (WHERE rn = 3) AS exp3 FROM monthly WHERE rn <= 3 GROUP BY symbol, trade_date) SELECT COUNT(*) FROM symbol_daily_metrics s JOIN expected e USING (symbol, trade_date) WHERE s.expiry_30d IS DISTINCT FROM e.exp1 OR s.expiry_60d IS DISTINCT FROM e.exp2 OR s.expiry_90d IS DISTINCT FROM e.exp3",
    "straddle_pnl.total_entry": "SELECT COUNT(*) FROM straddle_pnl WHERE skip_reason IS NULL AND ABS(total_entry - (call_entry + put_entry)) > 0.01",
    "straddle_pnl.total_exit": "SELECT COUNT(*) FROM straddle_pnl WHERE skip_reason IS NULL AND ABS(total_exit - (call_exit + put_exit)) > 0.01",
    "straddle_pnl.pnl_formula": "SELECT COUNT(*) FROM straddle_pnl WHERE skip_reason IS NULL AND ABS(pnl - (total_entry - total_exit)) > 0.01",
    "straddle_pnl.move_formula": "SELECT COUNT(*) FROM straddle_pnl WHERE skip_reason IS NULL AND ABS(underlying_move_pct - ((underlying_close - underlying_open) / NULLIF(underlying_open, 0) * 100.0)) > 0.01",
    "straddle_pnl.expiry_closest_30d": "WITH expected AS (SELECT symbol, trade_date, expiry_date FROM (SELECT DISTINCT symbol, trade_date, expiry_date, ROW_NUMBER() OVER (PARTITION BY symbol, trade_date ORDER BY ABS((expiry_date - trade_date) - 30), expiry_date) AS rn FROM options_historical WHERE expiry_date >= trade_date) x WHERE rn = 1) SELECT COUNT(*) FROM straddle_pnl sp JOIN expected e USING (symbol, trade_date) WHERE sp.skip_reason IS NULL AND sp.expiry_date IS DISTINCT FROM e.expiry_date",
    "straddle_pnl.same_strike_legs": "SELECT COUNT(*) FROM straddle_pnl sp JOIN options_historical ce ON ce.symbol = sp.symbol AND ce.trade_date = sp.trade_date AND ce.expiry_date = sp.expiry_date AND ce.strike_price = sp.atm_strike AND ce.option_type = 'CE' JOIN options_historical pe ON pe.symbol = sp.symbol AND pe.trade_date = sp.trade_date AND pe.expiry_date = sp.expiry_date AND pe.strike_price = sp.atm_strike AND pe.option_type = 'PE' WHERE sp.skip_reason IS NULL AND (ce.open IS NULL OR pe.open IS NULL OR ce.close IS NULL OR pe.close IS NULL)",
    "trading_calendar.empty": "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END FROM trading_calendar",
    "trading_calendar.market_data_mismatch": "WITH md AS (SELECT trade_date FROM equity_historical UNION SELECT trade_date FROM options_historical) SELECT COUNT(*) FROM trading_calendar c FULL JOIN md USING (trade_date) WHERE COALESCE(c.is_trading_day, FALSE) IS DISTINCT FROM (md.trade_date IS NOT NULL)",
    "symbol_universe.no_active": "SELECT CASE WHEN COUNT(*) FILTER (WHERE is_active) = 0 THEN 1 ELSE 0 END FROM symbol_universe",
    "symbol_universe.missing_loaded_symbols": "SELECT COUNT(*) FROM (SELECT DISTINCT symbol FROM options_historical UNION SELECT DISTINCT symbol FROM equity_historical) x LEFT JOIN symbol_universe su USING (symbol) WHERE su.symbol IS NULL",
    "symbol_universe.active_fno_missing_metadata": "SELECT COUNT(*) FROM symbol_universe WHERE is_active AND symbol_type = 'individual_securities' AND (company_name IS NULL OR isin IS NULL)",
    "events.invalid_type": "SELECT COUNT(*) FROM events WHERE event_type IS NULL OR event_type = ''",
    "symbol_aggregates.win_rate_formula": "WITH calc AS (SELECT symbol, ROUND(100.0 * COUNT(*) FILTER (WHERE is_winner) / NULLIF(COUNT(*), 0), 2) AS value FROM straddle_pnl WHERE skip_reason IS NULL GROUP BY symbol) SELECT COUNT(*) FROM symbol_aggregates sa JOIN calc USING (symbol) WHERE ABS(sa.win_rate - calc.value) > 0.01",
    "symbol_aggregates.vrp_win_rate_formula": "WITH calc AS (SELECT symbol, ROUND(100.0 * COUNT(*) FILTER (WHERE vrp > 0) / NULLIF(COUNT(vrp), 0), 2) AS value FROM symbol_daily_metrics GROUP BY symbol) SELECT COUNT(*) FROM symbol_aggregates sa JOIN calc USING (symbol) WHERE sa.vrp_win_rate IS DISTINCT FROM calc.value AND COALESCE(ABS(sa.vrp_win_rate - calc.value), 999) > 0.01",
    "symbol_aggregates.avg_pnl_formula": "WITH calc AS (SELECT symbol, AVG(pnl) AS value FROM straddle_pnl WHERE skip_reason IS NULL GROUP BY symbol) SELECT COUNT(*) FROM symbol_aggregates sa JOIN calc USING (symbol) WHERE ABS(sa.avg_straddle_pnl - calc.value) > 0.01",
    "symbol_aggregates.max_profit_formula": "WITH calc AS (SELECT symbol, MAX(pnl) AS value FROM straddle_pnl WHERE skip_reason IS NULL GROUP BY symbol) SELECT COUNT(*) FROM symbol_aggregates sa JOIN calc USING (symbol) WHERE ABS(sa.max_profit - calc.value) > 0.01",
    "symbol_aggregates.max_loss_formula": "WITH calc AS (SELECT symbol, MIN(pnl) AS value FROM straddle_pnl WHERE skip_reason IS NULL GROUP BY symbol) SELECT COUNT(*) FROM symbol_aggregates sa JOIN calc USING (symbol) WHERE ABS(sa.max_loss - calc.value) > 0.01",
    "symbol_aggregates.earnings_range": "SELECT COUNT(*) FROM symbol_aggregates WHERE earnings_win_rate < 0 OR earnings_win_rate > 100",
}

DIAGNOSTIC_QUERIES = {
    "metrics.skew_null_reasons": """
        WITH metric_nulls AS (
            SELECT symbol, trade_date, expiry_30d
            FROM symbol_daily_metrics
            WHERE skew_25 IS NULL
        ),
        chain AS (
            SELECT m.symbol, m.trade_date,
                   COUNT(o.*) AS chain_rows,
                   COUNT(o.*) FILTER (WHERE o.delta IS NOT NULL) AS delta_rows,
                   COUNT(o.*) FILTER (WHERE o.option_type = 'CE' AND ABS(o.delta - 0.25) <= 0.05) AS ce_25_delta_rows,
                   COUNT(o.*) FILTER (WHERE o.option_type = 'PE' AND ABS(o.delta + 0.25) <= 0.05) AS pe_25_delta_rows
            FROM metric_nulls m
            LEFT JOIN options_historical o
              ON o.symbol = m.symbol AND o.trade_date = m.trade_date AND o.expiry_date = m.expiry_30d
            GROUP BY m.symbol, m.trade_date
        )
        SELECT
            COUNT(*) AS rows,
            COUNT(*) FILTER (WHERE chain_rows = 0) AS no_chain_rows,
            COUNT(*) FILTER (WHERE chain_rows > 0 AND delta_rows = 0) AS no_delta_rows,
            COUNT(*) FILTER (WHERE delta_rows > 0 AND (ce_25_delta_rows = 0 OR pe_25_delta_rows = 0)) AS no_25_delta_pair,
            MIN(trade_date) AS first_date,
            MAX(trade_date) AS last_date
        FROM chain
    """,
    "metrics.large_day_over_day_moves": """
        WITH changes AS (
            SELECT symbol, trade_date, iv_30,
                   trade_date - LAG(trade_date) OVER (PARTITION BY symbol ORDER BY trade_date) AS calendar_gap,
                   ABS(iv_30 - LAG(iv_30) OVER (PARTITION BY symbol ORDER BY trade_date)) AS iv30_abs_change,
                   ABS(skew_25 - LAG(skew_25) OVER (PARTITION BY symbol ORDER BY trade_date)) AS skew25_abs_change
            FROM symbol_daily_metrics
        )
        SELECT
            COUNT(*) FILTER (WHERE calendar_gap <= 7 AND iv30_abs_change > 0.50) AS iv30_change_gt_50_vol_points,
            COUNT(*) FILTER (WHERE calendar_gap <= 7 AND skew25_abs_change > 0.75) AS skew25_change_gt_75_vol_points,
            MAX(iv30_abs_change) FILTER (WHERE calendar_gap <= 7) AS max_iv30_abs_change_continuous,
            MAX(skew25_abs_change) FILTER (WHERE calendar_gap <= 7) AS max_skew25_abs_change_continuous
        FROM changes
    """,
    "metrics.gaps_by_symbol": """
        WITH ordered AS (
            SELECT symbol, trade_date,
                   trade_date - LAG(trade_date) OVER (PARTITION BY symbol ORDER BY trade_date) AS calendar_gap
            FROM symbol_daily_metrics
        )
        SELECT symbol, COUNT(*) FILTER (WHERE calendar_gap > 7) AS gaps_gt_7_calendar_days
        FROM ordered
        GROUP BY symbol
        HAVING COUNT(*) FILTER (WHERE calendar_gap > 7) > 0
        ORDER BY gaps_gt_7_calendar_days DESC, symbol
        LIMIT 25
    """,
    "aggregates.result_null_reasons": """
        SELECT
            COUNT(*) AS aggregate_rows,
            COUNT(*) FILTER (WHERE historical_iv_crush IS NULL) AS historical_iv_crush_null,
            COUNT(*) FILTER (WHERE implied_result_move IS NULL) AS implied_result_move_null,
            COUNT(*) FILTER (WHERE avg_result_move IS NULL) AS avg_result_move_null,
            COUNT(*) FILTER (WHERE avg_earnings_pnl IS NULL) AS avg_earnings_pnl_null,
            COUNT(*) FILTER (WHERE ev.result_events IS NULL) AS symbols_without_result_events
        FROM symbol_aggregates sa
        LEFT JOIN (
            SELECT symbol, COUNT(*) AS result_events
            FROM events
            WHERE event_type = 'RESULT'
            GROUP BY symbol
        ) ev USING (symbol)
    """,
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate all database tables for nulls and range/formula errors.")
    parser.add_argument("--output", default="data/validation_database.json")
    args = parser.parse_args()

    repo = MarketRepository(await get_pool())
    try:
        tables = [
            row["table_name"]
            for row in await repo.pool.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
        ]
        table_counts = {}
        nulls = {}
        for table in tables:
            table_counts[table] = int(await repo.pool.fetchval(f"SELECT COUNT(*) FROM {table}") or 0)
            columns = await repo.pool.fetch(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1
                ORDER BY ordinal_position
                """,
                table,
            )
            nulls[table] = {}
            for column in columns:
                if column["is_nullable"] == "YES":
                    nulls[table][column["column_name"]] = int(
                        await repo.pool.fetchval(f"SELECT COUNT(*) FROM {table} WHERE {column['column_name']} IS NULL") or 0
                    )

        range_errors = {
            name: int(await repo.pool.fetchval(sql) or 0)
            for name, sql in RANGE_CHECKS.items()
        }
        diagnostics = {}
        for name, sql in DIAGNOSTIC_QUERIES.items():
            rows = await repo.pool.fetch(sql)
            diagnostics[name] = [dict(row) for row in rows]
        report = {
            "table_counts": table_counts,
            "nullable_column_null_counts": nulls,
            "range_error_counts": range_errors,
            "diagnostics": diagnostics,
            "failing_checks": {name: count for name, count in range_errors.items() if count},
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, default=str))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
