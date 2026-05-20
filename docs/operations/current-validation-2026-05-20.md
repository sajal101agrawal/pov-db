# Current Validation Report

Run date: 2026-05-20.

## Summary

The latest available NSE EOD bhavcopy in this local run is `2026-05-19`. `2026-05-20` is not treated
as complete EOD data yet.

## Rate Convention

`interest_rates` is now populated with:

| Source | Tenor | Min date | Max date | Rate | Rows |
|---|---|---:|---:|---:|---:|
| `fixed:nse_iv_10pct` | `91d` | `2019-12-23` | `2026-05-20` | `0.10` | `1,673` |

This follows NSE’s option-chain IV convention of using 10% interest rate.

## Five-Symbol Historical Recompute

Recomputed analytics for:

- `RELIANCE`
- `SBIN`
- `INFY`
- `HDFCBANK`
- `TCS`

Window:

- Start: `2021-05-20`
- End: `2026-05-19`
- Trading dates: `1,233`
- Symbol-days: `6,165`

## Last Five Trading-Day NSE Source Validation

Dates:

- `2026-05-13`
- `2026-05-14`
- `2026-05-15`
- `2026-05-18`
- `2026-05-19`

Checks:

- Equity OHLC matched source.
- Equity volume matched source.
- F&O row counts matched source.

Result:

- Mismatches: `0`
- Report: `data/validation_market_data_5symbols_5days.json`

## DTE Correction

DTE columns now match actual selected expiry minus trade date.

Example:

```text
RELIANCE 2026-05-19
expiry_30d = 2026-06-30
dte_30     = 42
expiry_60d = 2026-07-28
dte_60     = 70
expiry_90d = 2026-07-28
dte_90     = 70
```

The IV fields remain constant-maturity fields. Forward-vol formulas use target horizons `30` and
`60`; DTE columns are metadata for selected exchange expiries.

## Active F&O Universe

Refreshed from NSE F&O bhavcopy dated `2026-05-19`.

| Category | Count |
|---|---:|
| Active stock underlyings | 209 |
| Active index underlyings | 5 |
| Active total | 214 |
| Inactive historical symbols retained | 44 |
| Total `symbol_universe` rows | 258 |

## Trading Calendar

`trading_calendar` is now populated:

- Date range: `2020-01-01` to `2026-05-20`
- Rows: `2,332`
- Local trading days: `1,236`

## DB-Wide Validation

Report: `data/validation_database.json`.

Table counts:

| Table | Rows |
|---|---:|
| `options_historical` | 4,033,814 |
| `equity_historical` | 87,983 |
| `symbol_daily_metrics` | 13,355 |
| `straddle_pnl` | 13,355 |
| `symbol_aggregates` | 160 |
| `symbol_universe` | 258 |
| `interest_rates` | 1,673 |
| `events` | 299 |
| `expiry_calendar` | 1,236 |
| `trading_calendar` | 2,332 |
| `error_log` | 72 |
| `live_snapshot` | 0 |
| `pipeline_state` | 0 |

Failing validation checks:

- None.

## Error Log

`error_log` contains 72 source/bootstrap failures imported from historical bootstrap logs. These are
mostly expected non-bhavcopy dates such as holidays or dates where all source URLs returned no data.

Future API and pipeline errors now flow through the same table.
