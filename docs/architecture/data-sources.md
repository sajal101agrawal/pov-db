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

Result events are loaded from NSE corporate event-calendar API and stored in `events` with
`event_type = 'RESULT'` and `source = 'nse:event-calendar'`.

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

Live or intraday data should be added through the Dhan/live option-chain path when exact timed
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

For upcoming result calendars, NSE's public corporate calendar/disclosure pages do not always
present the forward-looking schedule in the detail needed for trading workflows. The preferred
production approach is:

- keep NSE/BSE filings as the authoritative source whenever exact filed disclosures are available;
- use a structured upcoming-results feed, such as Dhan's Stock Events Calendar or another licensed
  earnings-calendar provider, for forward scheduling;
- store the provider name in `events.source` so historical backtests can distinguish filed events
  from upcoming/planned events.

Sensibull-style calendars are useful operationally, but they should not be treated as the only
authoritative source unless there is a stable API/license for production ingestion.
