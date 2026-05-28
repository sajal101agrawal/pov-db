# Data Sources

## Bhavcopy

`BhavcopySource` tries Samco first and NSE archives second. Each HTTP call has retry with
exponential backoff for transient network/server failures. If the first source still fails,
the pipeline falls back to the next source and records full diagnostics when all sources fail.

Fallback order for F&O:

1. Samco `getBhavcopy`, segment `NSEFO`
2. NSE new archive naming: `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`
3. NSE legacy derivative archive naming: `foDDMMMYYYYbhav.csv.zip`

Fallback order for cash market:

1. Samco `getBhavcopy`, segment `NSE`
2. NSE new archive naming: `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`
3. NSE legacy equity archive naming: `cmDDMMMYYYYbhav.csv.zip`

## Risk-Free Rate

NSE documents on its option-chain page that a fixed `10%` interest rate is applied while
computing displayed implied volatility. This service therefore stores `0.10` in
`interest_rates.rate` with source `fixed:nse_iv_10pct`.

Do not use Yahoo `^IRX` for this project. `^IRX` is the US 13-week T-bill and caused a
currency/convention mismatch for Indian option IV.

The rate is stored as a decimal:

- `0.10` means `10%`
- It is an IV-calculation convention, not a live Indian treasury curve

## Equity Fallback

The primary equity source is NSE CM bhavcopy. `YahooFinanceClient.fetch_equity_history` is available
for bootstrap gaps and future repair jobs.

## Events

Result events are stored in `events` with `event_type = 'RESULT'`.

Sources:

- NSE corporate event-calendar API with `source = 'nse:event-calendar'`.
- Yahoo Finance earnings calendar with `source = 'yahoo:earnings-calendar'`.

NSE remains the source of truth for completed/filed board-meeting result dates. Yahoo is used
only for forward-looking scheduled earnings dates, and only the `Earnings Date` calendar field is
ingested so dividend dates and other corporate actions are excluded.

## Active F&O Universe

The active F&O universe is refreshed from the latest NSE F&O bhavcopy.

Metadata enrichment uses:

- NSE `EQUITY_L.csv` for company name, ISIN, and market lot.
- NSE index constituent CSVs for Nifty flags and partial industry coverage.
- Optional NSE `quote-equity` calls via `scripts/initialize_market_data.py --enrich-quote` for richer sector, industry, and tick-size values.

As of the 2026-05-20 refresh, the local DB has 214 active symbols: 209 stock underlyings
and 5 index underlyings.

## Trading Calendar

`trading_calendar` is derived from locally loaded NSE market data, not guessed from weekdays alone.
A date is a trading day when equity or F&O bhavcopy rows exist locally. Weekends are marked
`weekend`; missing weekdays are marked `no_local_bhavcopy` until data is loaded or the source
failure is investigated. This avoids incorrectly declaring an NSE holiday when the real issue is
a failed download.

## Historical Backtest Granularity

Historical option data is loaded from daily bhavcopy files. Each contract has daily OHLC fields,
not intraday timestamps. The straddle backtest therefore stores one row per symbol per trading day:

- entry price = option `OPEN`
- exit price = option `CLOSE`

Live or intraday data should be added through the live option-chain path when exact timed
morning/evening execution is required.

## Historical IV Cross-Validation

DhanHQ's Expired Options Data API (`POST /charts/rollingoption`) is the best external source to
cross-check historical IV because it exposes expired option OHLC, IV, OI, volume, strike, and spot
for up to the last five years. It can fetch up to 30 days in one call. Once Dhan credentials are
configured, use this feed to sample ATM call/put IV for the same symbol, expiry bucket, strike, and
date against `options_historical.iv` and `symbol_daily_metrics.iv_30`.

Reference: [DhanHQ Expired Options Data](https://dhanhq.co/docs/v2/expired-options-data/).

## Corporate Events

Historical result events currently come from NSE corporate-event data and are loaded into
`events` with `event_type='RESULT'`. That is the source of truth for completed/result-filed dates.

For upcoming result calendars, NSE does not consistently expose a useful forward schedule for
every active F&O symbol. The service supplements NSE with Yahoo Finance's earnings calendar:

- `scripts/daily_update.py` refreshes NSE and Yahoo result events unless `--skip-events` is used.
- `scripts/update_result_events.py` refreshes only result events, which is useful on weekends and
  market holidays when bhavcopy ETL should not run.
- Future `yahoo:earnings-calendar` rows are deleted and reinserted on each refresh for the target
  symbols, so changed Yahoo schedules do not leave stale planned dates behind.
- API consumers can distinguish filed versus planned dates using `events.source`.

The dashboard exposes the next upcoming result date as `result_date` and a fuller
`upcoming_events` array from `GET /api/all-dashboard`.
