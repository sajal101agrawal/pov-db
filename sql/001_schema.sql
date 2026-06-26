CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS pipeline_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS error_log (
    id BIGSERIAL PRIMARY KEY,
    task_name TEXT NOT NULL,
    symbol VARCHAR(20),
    trade_date DATE,
    source TEXT,
    error_type TEXT NOT NULL,
    error_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS trading_calendar (
    trade_date DATE PRIMARY KEY,
    is_trading_day BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS symbol_universe (
    symbol VARCHAR(20) PRIMARY KEY,
    company_name VARCHAR(150),
    isin VARCHAR(12),
    sector VARCHAR(100),
    industry VARCHAR(100),
    is_nifty50 BOOLEAN NOT NULL DEFAULT FALSE,
    is_nifty100 BOOLEAN NOT NULL DEFAULT FALSE,
    is_banknifty BOOLEAN NOT NULL DEFAULT FALSE,
    is_midcap BOOLEAN NOT NULL DEFAULT FALSE,
    symbol_type VARCHAR(30),
    lot_size INTEGER,
    tick_size NUMERIC(8,4),
    yahoo_symbol VARCHAR(40),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_su_universe ON symbol_universe (is_nifty50, is_nifty100, is_banknifty);
CREATE INDEX IF NOT EXISTS idx_su_type ON symbol_universe (symbol_type);

CREATE TABLE IF NOT EXISTS interest_rates (
    rate_date DATE NOT NULL,
    tenor VARCHAR(10) NOT NULL,
    rate NUMERIC(10,8) NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (rate_date, tenor)
);

CREATE INDEX IF NOT EXISTS idx_ir_date_desc ON interest_rates (rate_date DESC);

CREATE TABLE IF NOT EXISTS equity_historical (
    symbol VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    open NUMERIC(12,4),
    high NUMERIC(12,4),
    low NUMERIC(12,4),
    close NUMERIC(12,4),
    volume BIGINT,
    turnover NUMERIC(20,4),
    delivery_volume BIGINT,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
);

SELECT create_hypertable('equity_historical', 'trade_date', chunk_time_interval => INTERVAL '1 year', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_eh_symbol_date_desc ON equity_historical (symbol, trade_date DESC);

CREATE TABLE IF NOT EXISTS options_historical (
    symbol VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    expiry_date DATE NOT NULL,
    strike_price NUMERIC(12,2) NOT NULL,
    option_type CHAR(2) NOT NULL CHECK (option_type IN ('CE', 'PE')),
    instrument_type VARCHAR(10),
    open NUMERIC(12,4),
    high NUMERIC(12,4),
    low NUMERIC(12,4),
    close NUMERIC(12,4),
    settle_price NUMERIC(12,4),
    num_contracts INTEGER,
    contract_value NUMERIC(20,2),
    open_interest BIGINT,
    change_in_oi INTEGER,
    iv NUMERIC(10,8),
    delta NUMERIC(10,8),
    gamma NUMERIC(16,10),
    theta NUMERIC(16,8),
    vega NUMERIC(16,8),
    rho NUMERIC(16,8),
    days_to_expiry SMALLINT,
    is_atm BOOLEAN,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date, expiry_date, strike_price, option_type)
);

SELECT create_hypertable('options_historical', 'trade_date', chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_oh_symbol_date ON options_historical (symbol, trade_date);
CREATE INDEX IF NOT EXISTS idx_oh_expiry_chain ON options_historical (symbol, trade_date, expiry_date, option_type);
CREATE INDEX IF NOT EXISTS idx_oh_atm ON options_historical (symbol, trade_date) WHERE is_atm = TRUE;
CREATE INDEX IF NOT EXISTS idx_oh_delta_skew ON options_historical (symbol, trade_date, expiry_date, option_type, (abs(delta)));

ALTER TABLE options_historical SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,option_type',
    timescaledb.compress_orderby = 'trade_date DESC,expiry_date,strike_price'
);
SELECT add_compression_policy('options_historical', INTERVAL '60 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS expiry_calendar (
    symbol VARCHAR(20) NOT NULL,
    expiry_date DATE NOT NULL,
    expiry_type VARCHAR(10),
    instrument_type VARCHAR(10),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, expiry_date)
);

CREATE INDEX IF NOT EXISTS idx_ec_symbol_expiry ON expiry_calendar (symbol, expiry_date);

CREATE TABLE IF NOT EXISTS events (
    symbol VARCHAR(20) NOT NULL,
    event_date DATE NOT NULL,
    event_type VARCHAR(30) NOT NULL,
    description TEXT,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, event_date, event_type)
);

CREATE INDEX IF NOT EXISTS idx_ev_symbol_date ON events (symbol, event_date);
CREATE INDEX IF NOT EXISTS idx_ev_results ON events (event_date) WHERE event_type = 'RESULT';

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
);

CREATE INDEX IF NOT EXISTS idx_ca_symbol_ex_date ON corporate_actions (symbol, ex_date);
CREATE INDEX IF NOT EXISTS idx_ca_pending ON corporate_actions (ex_date, symbol)
    WHERE adjustment_status = 'PENDING_FACTOR';

CREATE TABLE IF NOT EXISTS symbol_daily_metrics (
    symbol VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    iv_30 NUMERIC(18,8),
    iv_60 NUMERIC(18,8),
    iv_90 NUMERIC(18,8),
    call_iv_30 NUMERIC(18,8),
    call_iv_60 NUMERIC(18,8),
    call_iv_90 NUMERIC(18,8),
    put_iv_30 NUMERIC(18,8),
    put_iv_60 NUMERIC(18,8),
    put_iv_90 NUMERIC(18,8),
    expiry_30d DATE,
    expiry_60d DATE,
    expiry_90d DATE,
    dte_30 SMALLINT,
    dte_60 SMALLINT,
    dte_90 SMALLINT,
    atm_strike NUMERIC(12,2),
    nearest_ce_iv NUMERIC(18,8),
    nearest_pe_iv NUMERIC(18,8),
    nearest_ce_ltp NUMERIC(12,4),
    nearest_pe_ltp NUMERIC(12,4),
    rv_10 NUMERIC(18,8),
    rv_20 NUMERIC(18,8),
    rv_30 NUMERIC(18,8),
    rv_60 NUMERIC(18,8),
    rv_90 NUMERIC(18,8),
    rv_10_raw NUMERIC(18,8),
    rv_20_raw NUMERIC(18,8),
    rv_30_raw NUMERIC(18,8),
    rv_60_raw NUMERIC(18,8),
    rv_90_raw NUMERIC(18,8),
    rv_data_status VARCHAR(40) NOT NULL DEFAULT 'LEGACY_UNVERIFIED',
    rv_adjustment_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    rv_calculation_version SMALLINT NOT NULL DEFAULT 1,
    vrp NUMERIC(18,8),
    vrp_signal_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    fwdv_3060 NUMERIC(18,8),
    fwdfct_3060 NUMERIC(18,8),
    call_fwdfct_3060 NUMERIC(18,8),
    put_fwdfct_3060 NUMERIC(18,8),
    fev_30 NUMERIC(18,8),
    iv_slope_3060 NUMERIC(18,8),
    skew_20 NUMERIC(18,8),
    skew_25 NUMERIC(18,8),
    skew_30 NUMERIC(18,8),
    smoothed_skew NUMERIC(18,8),
    iv30_rv30_ratio NUMERIC(18,8),
    iv30_fev30_ratio NUMERIC(18,8),
    avg_option_volume NUMERIC(20,4),
    daily_rsi NUMERIC(8,4),
    weekly_rsi NUMERIC(8,4),
    iv_30_percentile NUMERIC(6,2),
    iv_60_percentile NUMERIC(6,2),
    iv_90_percentile NUMERIC(6,2),
    call_fwdfct_3060_percentile NUMERIC(6,2),
    put_fwdfct_3060_percentile NUMERIC(6,2),
    vrp_percentile NUMERIC(6,2),
    skew_percentile NUMERIC(6,2),
    skew_rank SMALLINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    PRIMARY KEY (symbol, trade_date)
);

SELECT create_hypertable('symbol_daily_metrics', 'trade_date', chunk_time_interval => INTERVAL '1 year', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_sdm_latest ON symbol_daily_metrics (symbol, trade_date DESC) INCLUDE (iv_30, iv_60, iv_90, vrp, skew_25, rv_30);
CREATE INDEX IF NOT EXISTS idx_sdm_date_symbol ON symbol_daily_metrics (trade_date, symbol);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_60 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_iv_30 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_iv_60 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_iv_90 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_iv_30 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_iv_60 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_iv_90 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS call_fwdfct_3060 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS put_fwdfct_3060 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_90 NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_10_raw NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_20_raw NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_30_raw NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_60_raw NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_90_raw NUMERIC(18,8);
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_data_status VARCHAR(40) NOT NULL DEFAULT 'LEGACY_UNVERIFIED';
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_adjustment_details JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS rv_calculation_version SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE symbol_daily_metrics ADD COLUMN IF NOT EXISTS vrp_signal_enabled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS analytics_backfill_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS analytics_metric_audit (
    run_id UUID NOT NULL REFERENCES analytics_backfill_runs(run_id),
    symbol VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    old_values JSONB NOT NULL,
    new_values JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS straddle_pnl (
    symbol VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    expiry_date DATE,
    atm_strike NUMERIC(12,2),
    underlying_open NUMERIC(12,4),
    underlying_close NUMERIC(12,4),
    underlying_move_pct NUMERIC(10,6),
    call_entry NUMERIC(12,4),
    put_entry NUMERIC(12,4),
    total_entry NUMERIC(12,4),
    call_exit NUMERIC(12,4),
    put_exit NUMERIC(12,4),
    total_exit NUMERIC(12,4),
    pnl NUMERIC(12,4),
    is_winner BOOLEAN,
    has_result_event BOOLEAN NOT NULL DEFAULT FALSE,
    iv_on_entry NUMERIC(10,8),
    skip_reason VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
);

SELECT create_hypertable('straddle_pnl', 'trade_date', chunk_time_interval => INTERVAL '1 year', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_sp_symbol_date ON straddle_pnl (symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_sp_winner ON straddle_pnl (symbol, is_winner);
CREATE INDEX IF NOT EXISTS idx_sp_result_days ON straddle_pnl (symbol, has_result_event) WHERE has_result_event = TRUE;

CREATE TABLE IF NOT EXISTS symbol_aggregates (
    symbol VARCHAR(20) PRIMARY KEY,
    win_rate NUMERIC(6,2),
    vrp_win_rate NUMERIC(6,2),
    avg_vrp_4y NUMERIC(10,8),
    vrp_calculation_version SMALLINT NOT NULL DEFAULT 1,
    avg_straddle_pnl NUMERIC(12,4),
    avg_straddle_pnl_pct NUMERIC(10,6),
    avg_call_pnl NUMERIC(12,4),
    avg_put_pnl NUMERIC(12,4),
    max_profit NUMERIC(12,4),
    max_loss NUMERIC(12,4),
    historical_iv_crush NUMERIC(10,6),
    implied_result_move NUMERIC(10,6),
    avg_result_move NUMERIC(10,6),
    max_result_move NUMERIC(10,6),
    avg_earnings_pnl NUMERIC(12,4),
    earnings_win_rate NUMERIC(6,2),
    max_earnings_profit NUMERIC(12,4),
    max_earnings_loss NUMERIC(12,4),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS vrp_calculation_version SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS avg_earnings_pnl NUMERIC(12,4);
ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS earnings_win_rate NUMERIC(6,2);
ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS max_earnings_profit NUMERIC(12,4);
ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS max_earnings_loss NUMERIC(12,4);
ALTER TABLE symbol_aggregates ADD COLUMN IF NOT EXISTS avg_straddle_pnl_pct NUMERIC(10,6);

CREATE TABLE IF NOT EXISTS live_snapshot (
    symbol VARCHAR(20) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    current_price NUMERIC(12,4),
    pnl NUMERIC(12,4),
    maxloss NUMERIC(12,4),
    option_chain JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, snapshot_time)
);

SELECT create_hypertable('live_snapshot', 'snapshot_time', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_ls_symbol_time ON live_snapshot (symbol, snapshot_time DESC);

CREATE TABLE IF NOT EXISTS live_symbol_metrics (
    symbol VARCHAR(20) PRIMARY KEY,
    snapshot_time TIMESTAMPTZ NOT NULL,
    current_price NUMERIC(12,4),
    source VARCHAR(80),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lsm_snapshot_time ON live_symbol_metrics (snapshot_time DESC);

CREATE TABLE IF NOT EXISTS broker_access_tokens (
    provider VARCHAR(30) PRIMARY KEY,
    access_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
