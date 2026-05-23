# Retries, Failures, and Validation

Last validated locally: 2026-05-21.

## Retry Policy

HTTP source calls use exponential backoff for transient failures.

Settings:

| Setting | Default | Meaning |
|---|---:|---|
| `SOURCE_RETRY_ATTEMPTS` | `3` | Total attempts per HTTP call |
| `SOURCE_RETRY_BASE_DELAY_SECONDS` | `0.75` | Initial retry delay |
| `SOURCE_RETRY_MAX_DELAY_SECONDS` | `8.0` | Maximum retry delay |

Retryable failures:

- Connect timeout
- Read timeout
- Connection reset/read errors
- Remote protocol errors
- Pool timeout
- HTTP `408`, `425`, `429`, `500`, `502`, `503`, `504`

Non-retryable examples:

- HTTP `404` for NSE archive files. This normally means no bhavcopy for that date/source path.
- CSV parse errors after a successful download. These are logged and should be investigated.

## Source Fallback Order

F&O bhavcopy:

1. Samco `NSEFO`
2. NSE new archive URL
3. NSE legacy derivatives archive URL

Cash bhavcopy:

1. Samco `NSE`
2. NSE new archive URL
3. NSE legacy equities archive URL

If all sources fail, the pipeline raises a single error that includes all source diagnostics.

## Error Logging

Errors are stored in `error_log`.

Currently logged:

- `initialize_market_data` failures during historical runs.
- `daily_update` failures during daily ETL runs.
- `live_snapshot_worker` failures during Dhan live polling.
- `api_live_snapshot` failures from `/api/admin/live-snapshot`.
- `api_trigger_pipeline` failures from `/api/admin/trigger-pipeline`.
- Uncaught API errors via the global FastAPI exception handler.

The global handler returns:

```json
{
  "detail": "internal server error",
  "error_id": "<uuid>"
}
```

The same `error_id` is stored in `error_log.error_details`, so future email alerting can send that ID and traceback without changing every endpoint.

## Validation Scripts

### Validate Against NSE Source

Checks last N trading days against fresh NSE bhavcopy downloads:

```bash
python scripts/validate_market_data.py \
  --symbols RELIANCE,SBIN,INFY,HDFCBANK,TCS \
  --days 5 \
  --end 2026-05-20 \
  --output data/validation_market_data_5symbols_5days.json
```

What it checks:

- Equity OHLC exactly matches NSE source within `0.01`.
- Equity volume exactly matches NSE source.
- F&O row counts match NSE source per symbol/date.

Latest result:

- Dates: `2026-05-14`, `2026-05-15`, `2026-05-18`, `2026-05-19`, `2026-05-20`
- Symbols: `RELIANCE`, `SBIN`, `INFY`, `HDFCBANK`, `TCS`
- Mismatches: `0`

### Validate Database Ranges and Formula Invariants

```bash
python scripts/validate_database.py --output data/validation_database.json
```

Checks:

- Table row counts.
- Null counts for all nullable columns.
- Interest-rate ranges/source.
- Equity OHLC/volume ranges.
- Option price, OI, IV, delta, Greek ranges.
- Option `days_to_expiry` formula.
- Daily metrics vol/RSI/percentile ranges, including `rv_60` and `rv_90`.
- Daily metrics DTE equals selected expiry minus trade date.
- Straddle total and PnL formulas.
- Straddle expiry equals the `symbol_daily_metrics.expiry_30d` monthly bucket.
- Straddle entry/exit prices match the same-strike CE/PE bhavcopy open/close values.
- Straddle ATM strike is the nearest strike to underlying open.
- Symbol aggregate daily and earnings backtest formulas.
- Trading calendar not empty.
- Active symbol universe exists.
- All symbols seen in loaded equity/options history exist in `symbol_universe`.
- Active F&O equity symbols have at least `company_name` and `isin` after metadata refresh.
- Trading calendar flags match locally loaded equity/options dates.
- Diagnostic sections explain skew nulls, aggregate result nulls, large day-over-day metric moves, and symbol date gaps.
- Percentile diagnostics verify nulls are excluded from ranking denominators and current-null
  metrics remain null instead of being coerced to zero.

Latest result:

- Failing checks: none.

### Formula Repair Utilities

Future daily ETL uses the main pipeline formulas directly. If a historical formula changes and
already-loaded rows must be regenerated without re-downloading bhavcopies, use:

```bash
python scripts/recompute_rsi_metrics.py
python scripts/recompute_straddle_pnl.py
python scripts/validate_database.py --output data/validation_database.json
```

These are not one-off patch scripts; they are reusable repair utilities for derived fields.

## Expected Nulls

Nulls are not all defects. Current expected null classes:

- `equity_historical.delivery_volume`: not consistently present in the current NSE CM source.
- `options_historical.iv` and Greeks: no-solution/no-market contracts.
- `symbol_daily_metrics.rv_*`: insufficient historical close window.
- `symbol_daily_metrics.vrp`: missing current RV30 or missing 20-trading-day lagged IV30.
- `symbol_daily_metrics.weekly_rsi`: insufficient weekly close history.
- `symbol_aggregates` result/earnings fields: no result-event overlap with a valid previous
  trading-day entry and next trading-day exit.
- `symbol_universe.sector`: available from NSE quote metadata or index files, not from bhavcopy. Run `scripts/initialize_market_data.py --enrich-quote` to fill it where NSE exposes it.
- `live_snapshot`: empty until Dhan credentials are configured and the live worker/manual trigger succeeds.
- `pipeline_state`: reserved for future job state.

## Current Local Data Quality Snapshot

From `data/validation_database.json`:

| Table | Rows |
|---|---:|
| `options_historical` | 4,033,814 |
| `equity_historical` | 87,983 |
| `symbol_daily_metrics` | 13,355 |
| `straddle_pnl` | 13,355 |
| `interest_rates` | 1,673 |
| `events` | 299 |
| `trading_calendar` | 2,332 |
| `error_log` | 72 |

## Recovery Behavior

When a daily bhavcopy source fails:

1. The specific HTTP call retries.
2. If the source still fails, the next source is tried.
3. If all sources fail, the date is logged in `error_log`.
4. Bootstrap continues to the next date.
5. Daily/manual pipeline raises after logging, so operators see failure clearly.

For a single-day repair, rerun:

```bash
python scripts/daily_update.py --date YYYY-MM-DD --symbols RELIANCE,SBIN
```

For larger repairs:

```bash
python scripts/initialize_market_data.py --start YYYY-MM-DD --end YYYY-MM-DD --symbols RELIANCE,SBIN --force
python scripts/recompute_analytics.py --start YYYY-MM-DD --end YYYY-MM-DD --symbols RELIANCE,SBIN
python scripts/validate_database.py
```

## Fresh Server Initialization

Use one command for a new server:

```bash
python scripts/initialize_market_data.py --years 5 --enrich-quote
```

This loads date-by-date NSE bhavcopy data, computes contract IV/Greeks, daily metrics and straddle PnL, populates interest rates, refreshes the active F&O universe, enriches symbol metadata from NSE, loads result events from NSE plus upcoming earnings from Yahoo Finance, builds the local trading calendar, refreshes percentiles/aggregates, and logs every failed date in `error_log`.

For a faster first pass, omit `--enrich-quote`; company name and ISIN still come from NSE `EQUITY_L.csv`, while sector/industry coverage is lower.

## Daily Update

After NSE publishes the EOD bhavcopy, run:

```bash
python scripts/daily_update.py --date YYYY-MM-DD
```

The daily update loads F&O and cash bhavcopy, computes metrics and PnL for that date, refreshes percentiles/aggregates, updates the calendar, refreshes symbol metadata from bulk NSE files, and loads result events unless `--skip-events` is provided. Event loading includes NSE filed result events and Yahoo Finance upcoming earnings dates. Yahoo future rows are replaced on each refresh to avoid stale planned dates.

To refresh only result/earnings events without running market-data ETL, use:

```bash
python scripts/update_result_events.py
```

This is safe on weekends and holidays because it does not fetch bhavcopy or recompute analytics.
It supports `--skip-nse`, `--skip-yahoo`, and `--symbols RELIANCE,TCS`.

## Server Scripts

Use these scripts on a server:

```bash
scripts/deploy_server.sh
scripts/setup_server.sh
scripts/bootstrap_history.sh
scripts/install_daily_etl_cron.sh
python scripts/update_result_events.py
```

Defaults:

- `deploy_server.sh` is the one-command idempotent path. It creates `.env`, starts services,
  bootstraps only if the DB is empty, installs cron by default, and restarts API/worker.
- `setup_server.sh` creates `.env` if absent, builds containers, and waits for API health.
- `bootstrap_history.sh` runs `initialize_market_data.py --years 5`, validates the DB, clears Redis, and restarts the API.
- `install_daily_etl_cron.sh` installs a weekday cron at `22:30` server time. It runs daily ETL, validates the DB, and clears Redis cache. Override with `CRON_TIME="45 22 * * 1-5"`.
- `update_result_events.py` refreshes only NSE/Yahoo result events and can be run manually between
  ETL runs. Clear Redis afterward if dashboard clients should see the new dates immediately.

Docker Compose owns the multi-service deployment because a Dockerfile can only build one
container image. The app Dockerfile builds the API/worker image; `docker-compose.yml` wires
Postgres, Redis, API, worker, persistent volumes, and service health checks.
