# Retries, Failures, and Validation

Last validated locally: 2026-05-20.

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

- `bootstrap_history` failures during historical runs.
- `run_pipeline` failures from CLI manual runs.
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
  --end 2026-05-19 \
  --output data/validation_market_data_5symbols_5days.json
```

What it checks:

- Equity OHLC exactly matches NSE source within `0.01`.
- Equity volume exactly matches NSE source.
- F&O row counts match NSE source per symbol/date.

Latest result:

- Dates: `2026-05-13`, `2026-05-14`, `2026-05-15`, `2026-05-18`, `2026-05-19`
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
- Daily metrics vol/RSI/percentile ranges.
- Daily metrics DTE equals selected expiry minus trade date.
- Straddle total and PnL formulas.
- Trading calendar not empty.
- Active symbol universe exists.
- All symbols seen in loaded equity/options history exist in `symbol_universe`.
- Active F&O equity symbols have at least `company_name` and `isin` after metadata refresh.
- Trading calendar flags match locally loaded equity/options dates.
- Diagnostic sections explain skew nulls, aggregate result nulls, large day-over-day metric moves, and symbol date gaps.

Latest result:

- Failing checks: none.

## Expected Nulls

Nulls are not all defects. Current expected null classes:

- `equity_historical.delivery_volume`: not consistently present in the current NSE CM source.
- `options_historical.iv` and Greeks: no-solution/no-market contracts.
- `symbol_daily_metrics.rv_*`: insufficient historical close window.
- `symbol_daily_metrics.vrp`: missing 20-trading-day lagged RV30.
- `symbol_daily_metrics.weekly_rsi`: insufficient weekly close history.
- `symbol_aggregates` result fields: no result-event overlap for that symbol in loaded history. Run `scripts/load_events.py` before judging these fields.
- `symbol_universe.sector`: available from NSE quote metadata or index files, not from bhavcopy. Run `scripts/refresh_symbol_universe.py --enrich-quote` to fill it where NSE exposes it.
- `live_snapshot`: empty until live ingestion is implemented.
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

For historical gaps, rerun:

```bash
python scripts/run_pipeline.py --date YYYY-MM-DD --symbols RELIANCE,SBIN
```

For larger repairs:

```bash
python scripts/bootstrap_history.py --start YYYY-MM-DD --end YYYY-MM-DD --symbols RELIANCE,SBIN --force
python scripts/recompute_analytics.py --start YYYY-MM-DD --end YYYY-MM-DD --symbols RELIANCE,SBIN
python scripts/validate_database.py
```

## Fresh Server Initialization

Use one command for a new server:

```bash
python scripts/initialize_market_data.py --years 4 --enrich-quote
```

This loads date-by-date NSE bhavcopy data, computes contract IV/Greeks, daily metrics and straddle PnL, populates interest rates, refreshes the active F&O universe, enriches symbol metadata from NSE, loads result events, builds the local trading calendar, refreshes percentiles/aggregates, and logs every failed date in `error_log`.

For a faster first pass, omit `--enrich-quote`; company name and ISIN still come from NSE `EQUITY_L.csv`, while sector/industry coverage is lower.

## Daily Update

After NSE publishes the EOD bhavcopy, run:

```bash
python scripts/daily_update.py --date YYYY-MM-DD
```

The daily update loads F&O and cash bhavcopy, computes metrics and PnL for that date, refreshes percentiles/aggregates, updates the calendar, refreshes symbol metadata from bulk NSE files, and loads result events unless `--skip-events` is provided.
