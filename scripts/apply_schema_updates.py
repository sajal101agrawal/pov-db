from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.pool import close_pool, get_pool


STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS corporate_actions (
        id BIGSERIAL PRIMARY KEY,
        symbol VARCHAR(20) NOT NULL,
        ex_date DATE NOT NULL,
        record_date DATE,
        action_type VARCHAR(30) NOT NULL,
        description TEXT NOT NULL,
        face_value NUMERIC(18,8),
        price_multiplier NUMERIC(20,12),
        cash_amount NUMERIC(18,8),
        rights_new_shares NUMERIC(18,8),
        rights_held_shares NUMERIC(18,8),
        subscription_price NUMERIC(18,8),
        adjustment_status VARCHAR(30) NOT NULL DEFAULT 'PENDING_FACTOR',
        factor_source VARCHAR(50),
        source TEXT NOT NULL,
        source_key TEXT NOT NULL,
        raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ,
        UNIQUE (source, source_key),
        CHECK (price_multiplier IS NULL OR price_multiplier > 0),
        CHECK (adjustment_status IN ('VERIFIED', 'PENDING_FACTOR', 'IGNORED'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ca_symbol_ex_date ON corporate_actions (symbol, ex_date)",
    "CREATE INDEX IF NOT EXISTS idx_ca_pending ON corporate_actions (ex_date, symbol) WHERE adjustment_status = 'PENDING_FACTOR'",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_60 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_iv_30 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_iv_60 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_iv_90 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_iv_30 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_iv_60 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_iv_90 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_fwdfct_3060 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_fwdfct_3060 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_fwdfct_3060_percentile NUMERIC(6,2)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_fwdfct_3060_percentile NUMERIC(6,2)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_90 NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_10_raw NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_20_raw NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_30_raw NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_60_raw NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_90_raw NUMERIC(18,8)",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_data_status VARCHAR(40) NOT NULL DEFAULT 'LEGACY_UNVERIFIED'",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_adjustment_details JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_calculation_version SMALLINT NOT NULL DEFAULT 1",
    "ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS vrp_signal_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS avg_earnings_pnl NUMERIC(12,4)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS earnings_win_rate NUMERIC(6,2)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS max_earnings_profit NUMERIC(12,4)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS max_earnings_loss NUMERIC(12,4)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS avg_straddle_pnl_pct NUMERIC(10,6)",
    "ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS vrp_calculation_version SMALLINT NOT NULL DEFAULT 1",
    """
    CREATE TABLE IF NOT EXISTS analytics_backfill_runs (
        run_id UUID PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        status VARCHAR(20) NOT NULL,
        parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
        summary JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analytics_metric_audit (
        run_id UUID NOT NULL REFERENCES analytics_backfill_runs(run_id),
        symbol VARCHAR(20) NOT NULL,
        trade_date DATE NOT NULL,
        old_values JSONB NOT NULL,
        new_values JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (run_id, symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_symbol_metrics (
        symbol VARCHAR(20) PRIMARY KEY,
        snapshot_time TIMESTAMPTZ NOT NULL,
        current_price NUMERIC(12,4),
        source VARCHAR(80),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lsm_snapshot_time ON live_symbol_metrics (snapshot_time DESC)",
    """
    CREATE TABLE IF NOT EXISTS broker_access_tokens (
        provider VARCHAR(30) PRIMARY KEY,
        access_token TEXT NOT NULL,
        expires_at TIMESTAMPTZ,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]


async def main() -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            for statement in STATEMENTS:
                await conn.execute(statement)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
