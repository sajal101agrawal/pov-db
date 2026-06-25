# Database Dictionary

Last validated locally: 2026-06-23.

## Unit Policy

| Field family | Stored unit | Notes |
|---|---|---|
| IV/RV/VRP/skew/forward vol | Decimal annualized vol | `0.265` means `26.5%` |
| Risk-free rate | Decimal annualized rate | NSE-IV convention is `0.10` |
| RSI | `0..100` scale | Not decimal |
| Percentiles/ranks | Percentile `0..100`, rank integer | Not decimal |
| Win rates | Percent points `0..100` | Not decimal |
| `underlying_move_pct` | Percent points | `-1.19` means `-1.19%` |
| PnL and prices | Rupees/index points | Same units as bhavcopy |

## Tables

### `options_historical`

Purpose: raw NSE F&O option-chain rows plus contract-level IV and Greeks.

Primary key: `(symbol, trade_date, expiry_date, strike_price, option_type)`.

Important columns:

- `instrument_type`: `OPTSTK` or `OPTIDX`.
- `open/high/low/close/settle_price`: raw bhavcopy option prices.
- `num_contracts`, `contract_value`, `open_interest`, `change_in_oi`: raw liquidity/OI fields.
- `iv`: BSM implied volatility, decimal.
- `delta/gamma/theta/vega/rho`: BSM Greeks. Stored `NULL` when IV cannot be solved.
- `days_to_expiry`: actual `expiry_date - trade_date`.
- `is_atm`: true for rows whose strike is nearest same-day spot close.

Expected nulls:

- `iv` and Greeks can be null for no-trade or no-solution contracts.
- `is_atm` can be null for rows that have not been through the derived-metrics pass.

Validation rules:

- Prices cannot be negative.
- OHLC containment is enforced only for traded contracts (`num_contracts > 0`) because NSE bhavcopy can carry zero-volume rows with `open/high/low = 0` and nonzero close/settle.
- `days_to_expiry` must equal `GREATEST(expiry_date - trade_date, 0)`.
- CE delta must be `0..1`; PE delta must be `-1..0`.

### `equity_historical`

Purpose: raw NSE cash-market OHLCV for underlying symbols.

Primary key: `(symbol, trade_date)`.

Used for:

- Spot input to IV and Greeks.
- RV and RSI.
- Straddle entry/exit spot context.

Expected nulls:

- `delivery_volume` is null because the current new-format NSE CM bhavcopy path does not provide it consistently.

Validation rules:

- OHLC values must be positive and internally consistent.
- Volume/turnover cannot be negative.

### `interest_rates`

Purpose: risk-free-rate input for IV calculations.

Current convention:

- Tenor: `91d`
- Source: `fixed:nse_iv_10pct`
- Rate: `0.10`

Reason: NSE option-chain notes state that a 10% interest rate is applied while computing IV. This table should not use Yahoo `^IRX`, because that is the US 13-week T-bill.

### `symbol_daily_metrics`

Purpose: one dashboard-ready row per symbol per trade date.

Important columns:

- `iv_30/iv_60/iv_90`: synthetic constant-maturity IVs, decimal annualized.
- `call_iv_30/call_iv_60/call_iv_90`: synthetic constant-maturity ATM call IVs.
- `put_iv_30/put_iv_60/put_iv_90`: synthetic constant-maturity ATM put IVs.
- `expiry_30d/expiry_60d/expiry_90d`: first/second/third monthly exchange-expiry buckets.
- `dte_30/dte_60/dte_90`: actual calendar days from `trade_date` to the selected expiry.
- `rv_10/rv_20/rv_30/rv_60/rv_90`: Yang-Zhang realized vol, decimal annualized.
- `rv_10_raw/.../rv_90_raw`: unadjusted diagnostic Yang-Zhang values.
- `rv_data_status`: RV30 reliability/adjustment state used by VRP.
- `rv_adjustment_details`: compact per-window corporate-action and gap diagnostics.
- `rv_calculation_version`: calculation lineage; version 2 is corporate-action aware.
- `vrp_signal_enabled`: true only when adjusted RV30 and lagged IV30 are usable.
- `vrp`: `iv_30(20 trading days earlier) - rv_30(today)`.
- `fwdv_3060`: synthetic forward volatility between target 30D and 60D maturities.
- `fwdfct_3060`: Average Forward Factor, `(iv_30 / fwdv_3060) - 1`. The database
  name is retained for compatibility.
- `call_fwdfct_3060`: Call Forward Factor from `call_iv_30` and `call_iv_60`.
- `put_fwdfct_3060`: Put Forward Factor from `put_iv_30` and `put_iv_60`.
- `iv_slope_3060`: `(iv_60 - iv_30) / 30`.
- `skew_20/25/30`: put IV minus call IV at the closest target deltas.
- `avg_option_volume`: total traded option contracts for the symbol/date, summed across all
  CE and PE contracts in `options_historical`.
- `daily_rsi/weekly_rsi`: RSI on `0..100`.
- Percentile fields: `0..100`.

Expected nulls:

- RV fields are null until enough historical closes exist.
- VRP is null until current RV30 and lagged IV30 exist.
- Weekly RSI is null until enough weekly closes exist.
- Percentiles are null when the current value is null. Non-null percentile calculations rank
  against the trailing available non-null observations for the same symbol; null observations
  are not counted in the percentile denominator.
- `nearest_ce_iv` can be null if the selected ATM call leg has no valid IV.
- Call/put IV and factor fields can be null independently when that option side lacks a usable IV
  or produces negative forward variance.

### `corporate_actions`

Purpose: source-versioned NSE price-adjusting events and verified pre-ex-date OHLC multipliers.
Ambiguous factors remain `PENDING_FACTOR`; affected canonical RV/VRP is disabled rather than
silently using raw prices.

### `analytics_backfill_runs` / `analytics_metric_audit`

Purpose: production recomputation lineage and old/new values for materially changed historical
metrics.

Validation rules:

- Volatility fields must be in a practical `0..5` decimal range.
- RSI and percentiles must be `0..100`.
- DTE fields must match selected expiry minus trade date.

### `straddle_pnl`

Purpose: daily short ATM straddle backtest.

Method:

- Entry spot: underlying open.
- Expiry: `symbol_daily_metrics.expiry_30d`, the first monthly exchange-expiry bucket.
- Strike: nearest strike to underlying open.
- Entry: CE open + PE open.
- Exit: CE close + PE close.
- PnL: entry total - exit total.
- `underlying_move_pct`: same-day underlying open-to-close move in percent points. It is stored
  for analysis/context and is not used to calculate option PnL.

Historical granularity:

- NSE bhavcopy is one EOD file per trading day, so the historical strategy uses contract `OPEN`
  as morning entry proxy and contract `CLOSE` as EOD exit proxy.
- There is no intraday timestamped entry/exit until a live or intraday option-chain source is added.
- Daily straddle PnL uses same-day option `OPEN` and `CLOSE`; previous-day entry logic applies
  only to earnings-event aggregates.

Expected nulls:

- `skip_reason` is null for valid rows.
- If data is missing, `skip_reason` is populated and leg fields may be null.

Validation rules:

- `total_entry = call_entry + put_entry`.
- `total_exit = call_exit + put_exit`.
- `pnl = total_entry - total_exit`.
- `underlying_move_pct = (close - open) / open * 100`.

### `symbol_aggregates`

Purpose: one row per symbol derived from `straddle_pnl` and `symbol_daily_metrics`.

Daily aggregate formulas:

- `win_rate`: valid daily short-straddle rows with `pnl > 0`, divided by valid daily rows.
- `vrp_win_rate`: reliable metric rows with `vrp > 0`, divided by reliable non-null VRP rows.
- `avg_vrp_4y`: legacy column name; currently averages all reliable loaded lookback rows. In the
  deployment run this is five years because the bootstrap lookback is five years.
- `vrp_calculation_version`: minimum RV/VRP lineage version included in the aggregate; APIs hide
  aggregate VRP fields until the full historical series is on version 2.
- `avg_straddle_pnl`, `avg_call_pnl`, `avg_put_pnl`: rupee averages over valid daily straddle rows.
- `avg_straddle_pnl_pct`: average `pnl / total_entry` over valid daily straddle rows, stored as a
  decimal ratio (`0.0123` means `1.23%`).

Earnings aggregate formulas:

- Entry date: previous loaded trading day before a `RESULT` event.
- Exit date: next loaded trading day after the event.
- Entry premium: entry-date EOD short-straddle credit, stored as `straddle_pnl.total_exit`
  because the daily table names same-day close as the daily exit value.
- Exit premium: same strike and expiry CE/PE closes on the exit date.
- `avg_earnings_pnl`: average `entry_premium - exit_premium`, short-straddle direction.
- `historical_iv_crush`: average `(entry_iv30 - exit_iv30) / entry_iv30`.
- `implied_result_move`: average `entry_premium / entry_underlying_close`.
- `avg_result_move`: average absolute underlying close-to-close move between entry and exit.

Expected nulls:

- Result-event analytics are null for symbols with no result-event overlap in loaded history.
- Earnings strategy aggregate fields are null when there is no valid result event with a
  previous trading-day entry and next trading-day exit using the same ATM straddle legs.

### `symbol_universe`

Purpose: active NSE F&O symbol registry.

Current refresh:

- Source: latest NSE F&O bhavcopy.
- Active universe on 2026-05-20: 214 symbols, including 209 stock underlyings and 5 index underlyings.

Expected nulls:

- Company metadata, sector, ISIN, lot size, and tick size are not available from the bhavcopy-only refresh and remain null until a separate master-data source is added.

### `trading_calendar`

Purpose: local trading/non-trading date lookup.

Current population:

- Dates with local equity bhavcopy rows are marked trading days.
- Weekends are non-trading.
- Weekdays with no local bhavcopy are marked `no_local_bhavcopy`.

### `events`

Purpose: corporate event calendar, mainly result dates.

Current sources:

- NSE event-calendar API stores filed/completed result board-meeting dates with
  `source = 'nse:event-calendar'`.
- Yahoo Finance earnings calendar stores forward-looking planned earnings dates with
  `source = 'yahoo:earnings-calendar'`.
- Result events are stored as `event_type = 'RESULT'`.

Operational notes:

- `daily_update.py` refreshes result events unless `--skip-events` is used.
- `update_result_events.py` refreshes only result events without running market-data ETL.
- Future Yahoo rows are replaced on each refresh for the target symbols, so stale planned dates are
  removed when Yahoo changes its schedule.
- Historical earnings aggregates only materialize when surrounding equity/options data exists, so
  future planned events do not affect completed backtest results until their market data is loaded.

### `error_log`

Purpose: durable operational error capture for pipeline/API/source failures.

Current usage:

- Bootstrap source failures are logged.
- Manual pipeline API failures are logged.
- Global API exception handler logs uncaught errors.

Future email alerts should hook into this table or the global exception handler without changing individual endpoints.

### `live_snapshot`

Purpose: append-only PostgreSQL audit table for full live option-chain snapshots.

Current write path:

- `scripts/live_snapshot_worker.py` polls the configured live quote provider during the configured
  IST market window.
- `POST /api/admin/live-snapshot` can trigger a manual option-chain snapshot using the configured
  option-chain provider. NSE is the default provider and does not require Dhan credentials.
- The same normalized payload is written to Redis for the live API and to PostgreSQL for audit.

### `live_symbol_metrics`

Purpose: latest per-symbol live payload used as the PostgreSQL fallback after Redis live keys expire.

Current write path:

- `fetch_and_store_live_quotes` writes each normalized live quote and option-summary payload to
  Redis and upserts the same payload into `live_symbol_metrics`.
- The row is replaced only when the incoming `snapshot_time` is newer than or equal to the stored
  snapshot.
- `/api/live`, `/api/live/{symbol}`, `/api/all-dashboard`, `/api/symbol/{symbol}`,
  `/api/symbol/{symbol}/history`, `/api/symbol/{symbol}/term-structure`, and
  `/api/symbol/{symbol}/volatility-cone` read Redis first and then this table. This preserves the
  last collected live market snapshot after market close instead of falling back to prior-day EOD
  data.

### `broker_access_tokens`

Purpose: latest short-lived broker access token used by live providers.

Current write path:

- `POST /api/admin/kite/session` exchanges a fresh Kite `request_token` from the JSON body, stores
  the resulting access token here, and also caches it in Redis.
- The live worker reads Redis first, then this table, then static env tokens.
- Kite tokens are still daily session tokens and expire the next morning per Kite's rules; this
  table is persistence across API/worker restarts, not a bypass for the daily login requirement.

### `pipeline_state`

Purpose: future job-state/checkpoint table.

Current status:

- Empty in local bootstrap.
