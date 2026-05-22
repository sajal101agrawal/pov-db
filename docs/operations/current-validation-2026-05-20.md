# Current Validation Report

Run date: 2026-05-21.

## Summary

The latest available NSE EOD bhavcopy in this local run is `2026-05-20`.

## Rate Convention

`interest_rates` is now populated with:

| Source | Tenor | Min date | Max date | Rate | Rows |
|---|---|---:|---:|---:|---:|
| `fixed:nse_iv_10pct` | `91d` | `2019-12-23` | `2026-05-21` | `0.10` | `1,674` |

This follows NSE’s option-chain IV convention of using 10% interest rate.

## All-Symbol Historical Load

Window:

- Start: `2021-05-20`
- End: `2026-05-20`
- Latest trade date: `2026-05-20`
- Metric symbol-days: `239,508`
- Valid/repaired straddle rows: `238,849`

The historical run loaded all NSE F&O symbols available in the bhavcopies, refreshed symbol
metadata, rebuilt daily metrics, recomputed RSI with Wilder smoothing, recomputed straddle PnL
from the corrected `expiry_30d` bucket, and refreshed symbol aggregates.

RV/VRP changes included in this recompute:

- RV uses Yang-Zhang, not close-to-close.
- `rv_60` and `rv_90` columns are present and populated where enough OHLC history exists.
- VRP uses the shifted-IV convention: `iv_30(t-20 trading days) - rv_30(t)`.

## Last Five Trading-Day NSE Source Validation

Dates:

- `2026-05-14`
- `2026-05-15`
- `2026-05-18`
- `2026-05-19`
- `2026-05-20`

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
RELIANCE 2021-06-14
expiry_30d = 2021-06-24
dte_30     = 10
expiry_60d = 2021-07-29
dte_60     = 45
expiry_90d = 2021-08-26
dte_90     = 73
```

The IV fields remain constant-maturity fields. Forward-vol formulas use target horizons `30` and
`60`; DTE columns are metadata for selected exchange expiries.

## Active F&O Universe

Refreshed from NSE F&O bhavcopy dated `2026-05-20`.

| Category | Count |
|---|---:|
| Active total | 214 |
| Total `symbol_universe` rows | 3,273 |

## Trading Calendar

`trading_calendar` is now populated:

- Date range: `2020-01-01` to `2026-05-20`
- Rows: `2,332`
- Local trading days: loaded equity/options dates match calendar trading flags
- Partial local trading days: `0`

## DB-Wide Validation

Report: `data/validation_database.json`.

Table counts:

| Table | Rows |
|---|---:|
| `options_historical` | 59,829,340 |
| `equity_historical` | 2,605,593 |
| `symbol_daily_metrics` | 239,508 |
| `straddle_pnl` | 239,511 |
| `symbol_aggregates` | 293 |
| `symbol_universe` | 3,273 |
| `interest_rates` | 1,674 |
| `events` | 9,861 |
| `expiry_calendar` | 13,465 |
| `trading_calendar` | 2,332 |
| `error_log` | 144 |
| `live_snapshot` | 0 |
| `pipeline_state` | 0 |

Failing validation checks:

- None.

Outlier diagnostics:

- Continuous IV jumps over 50 vol points: `9`
- Continuous skew jumps over 75 vol points: `5`
- DB-wide IV/skew/range formula failures: `0`
- Straddle expiry/leg/ATM-strike formula failures: `0`
- Split-like RV windows above the validation range were nulled and are now reported as expected
  RV nulls rather than outlier values.

Targeted formula checks:

- Latest RELIANCE VRP rows matched `iv_30(t-20) - rv_30(t)` exactly to 10 decimal places.
- `symbol_aggregates` daily PnL, win-rate, VRP win-rate, max-profit, and max-loss formula checks passed.
- Earnings aggregate formulas and ranges passed; null earnings aggregates are restricted to symbols
  without usable result-event entry/exit windows.

Latest RELIANCE RV/VRP sample:

| Date | RV30 | RV60 | RV90 | VRP |
|---|---:|---:|---:|---:|
| 2026-05-20 | 0.26097670 | 0.27007273 | 0.27893586 | -0.01383169 |
| 2026-05-19 | 0.26166001 | 0.26868032 | 0.27772125 | -0.02541407 |
| 2026-05-18 | 0.27556909 | 0.26891324 | 0.27784269 | -0.03307693 |

Earnings aggregate sample:

| Symbol | Avg earnings PnL | Earnings win rate | Implied result move | Avg result move |
|---|---:|---:|---:|---:|
| HDFCBANK | 6.2800 | 80.00 | 0.061850 | 0.018518 |
| INFY | -2.7667 | 55.56 | 0.069376 | 0.043146 |
| RELIANCE | 5.4158 | 68.42 | 0.060851 | 0.028748 |
| SBIN | -2.4600 | 65.00 | 0.065184 | 0.029877 |
| TCS | 25.6971 | 82.35 | 0.048945 | 0.024456 |

## Latest Straddle Sample

For `RELIANCE`:

```text
2026-05-20 expiry=2026-05-26 dte=6 total_entry=41.90 total_exit=49.85 pnl=-7.95
```

Formula check:

```text
pnl = total_entry - total_exit = 41.90 - 49.85 = -7.95
```

## Error Log

`error_log` contains 144 source/bootstrap failures from historical bootstrap runs. These are
mostly expected non-bhavcopy dates such as holidays or dates where all source URLs returned no data.

Future API and pipeline errors now flow through the same table.
