# Live Market Data Architecture

Last updated: 2026-05-28.

## Current State

Historical EOD option-chain data is implemented through NSE/Samco bhavcopy ingestion. Live market
data runs as the `worker` container in Docker Compose and uses free sources by default.

There are two live paths:

- All-symbol basic quotes: the worker polls Yahoo Finance chart quotes for all active F&O symbols
  during the IST market window and caches results in Redis. The quote payload is enriched with NSE
  option-chain summaries when `LIVE_OPTION_SUMMARY_PROVIDER=nse`.
- Symbol-specific full chain: `GET /api/live/{symbol}/option-chain` fetches NSE `option-chain-v3`
  by default and caches the chain payload. Successful option-chain fetches are also inserted into
  `live_snapshot` for audit/history.

## Recommended Provider

The default live stack is:

- Yahoo Finance chart endpoint for underlying prices.
- NSE `option-chain-v3` for live option volume, ATM IV, IV term structure, and full option-chain
  snapshots.
- Dhan is optional only when `LIVE_QUOTE_PROVIDER=dhan` or `LIVE_OPTION_CHAIN_PROVIDER=dhan` is
  explicitly configured. For full option-chain snapshots, Dhan failures are logged and the API
  falls back to NSE so stale credentials do not break `/api/live/{symbol}/option-chain`.

Config placeholders are already in `.env.example`:

```text
LIVE_QUOTE_PROVIDER=yahoo
LIVE_OPTION_SUMMARY_PROVIDER=nse
LIVE_OPTION_CHAIN_PROVIDER=nse
LIVE_MARKET_QUOTE_MIN_INTERVAL_SECONDS=1.0
LIVE_OPTION_CHAIN_MIN_INTERVAL_SECONDS=3.0
LIVE_SYMBOLS=RELIANCE,SBIN,INFY,HDFCBANK,TCS,NIFTY,BANKNIFTY
LIVE_POLL_INTERVAL_SECONDS=180
LIVE_MARKET_START_IST=09:00
LIVE_MARKET_END_IST=16:00
```

Dhan credentials can remain blank for the default local live path.

## Implementation Shape

Current split:

1. `app/sources/yahoo.py`: free quote client for live underlying prices.
2. `app/sources/nse_option_chain.py`: free NSE option-chain summary and full-chain client.
3. `scripts/live_snapshot_worker.py`: polls all active symbols for basic quotes, normalizes the response, and writes latest payloads to Redis.
4. API read path:
   - `GET /api/live`: latest basic quote payloads for all cached active symbols.
   - `GET /api/live/{symbol}`: latest basic quote payload for one symbol.
   - `GET /api/symbol/{symbol}/term-structure`: cached EOD term-structure history with the
     latest live IV/factor/slope row overlaid from Redis when available.
   - `GET /api/live/{symbol}/option-chain`: cached full option chain, fetching NSE on demand if missing.
   - `POST /api/admin/live-quotes`: manual basic quote refresh.
   - `POST /api/admin/live-snapshot`: manual option-chain snapshot refresh for selected symbols.

That architecture avoids broker credentials for the default local live path and lets
email/error alerting reuse the existing `error_log` global handler path.

## Unit Policy

NSE reports option-chain `impliedVolatility` as a percent-style number. Before combining it with
historical DB values, normalize it to decimal volatility:

```text
11.94 from NSE -> 0.1194 in DB/API analytics
```

Prices, OI, volume, and bid/ask can be stored as reported after numeric validation.

Historical contract IV/Greeks are still recalculated internally from NSE bhavcopy using
Black-Scholes-Merton. Live NSE full-chain snapshots do not provide Greeks in this integration, so
Greeks remain `NULL` for those live snapshot legs.

The live quote payload overlays NSE option-chain IV analytics onto the latest EOD baseline:

- `iv_30`, `iv_60`, and `iv_90` are recomputed from live ATM IV across the 30/60/90 expiry hints.
- `fwdv_3060`, `fwdfct_3060`, `fev_30`, and `iv_slope_3060` reuse the same formula helpers as the
  EOD pipeline.
- EOD values are preserved as `eod_iv_30`, `eod_iv_60`, `eod_iv_90`, `eod_fwdv_3060`,
  `eod_fwdfct_3060`, `eod_fev_30`, and `eod_iv_slope_3060` whenever live values are present.
- Source markers such as `iv_term_structure_source='nse:option-chain-v3'` and
  `forward_analytics_source='nse:option-chain-v3'` identify live overlays.

DhanHQ can still be used later as an optional external validator for historical IV if credentials
are configured, but it is no longer required for live operation.

## Operation

Start/restart the worker:

```bash
docker compose up -d --build worker
```

Trigger one manual basic quote refresh:

```bash
curl -X POST "http://localhost:8001/api/admin/live-quotes?symbols=RELIANCE,SBIN"
curl "http://localhost:8001/api/live"
curl "http://localhost:8001/api/live/RELIANCE"
```

Fetch and cache a full option chain for a symbol:

```bash
curl "http://localhost:8001/api/live/RELIANCE/option-chain"
```

The default market window is `09:00` to `16:00` IST on weekdays. Outside this window the
worker remains alive but does not poll live quotes. On-demand NSE option-chain API calls do not
require Dhan credentials.

## Remaining Work

- Add email alerting in the existing global/error-log path.
- Add broker-neutral live-provider abstraction if a second provider is introduced.
