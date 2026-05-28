# Live Market Data Architecture

Last updated: 2026-05-28.

## Current State

Historical EOD option-chain data is implemented through NSE/Samco bhavcopy ingestion. Live market
data is implemented through DhanHQ v2 and runs as the `worker` container in Docker Compose.

There are two live paths:

- All-symbol basic quotes: the worker polls Dhan Market Quote for all active F&O symbols during
  the IST market window and caches results in Redis. The quote payload is also enriched with NSE
  option-chain summaries when `LIVE_OPTION_SUMMARY_PROVIDER=nse`.
- Symbol-specific full chain: `GET /api/live/{symbol}/option-chain` fetches Dhan Option Chain
  on demand and caches the chain payload. Successful option-chain fetches are also inserted into
  `live_snapshot` for audit/history.

## Recommended Provider

DhanHQ v2 is the cleanest fit for the current live phase because Market Quote supports batched
basic quote requests for many instruments, while Option Chain returns the full chain for an
underlying and expiry, including OI, Greeks, volume, top bid/ask, last price, and IV.

The official docs define:

- Dhan Market Quote: `POST https://api.dhan.co/v2/marketfeed/quote`
- `POST https://api.dhan.co/v2/optionchain/expirylist`
- `POST https://api.dhan.co/v2/optionchain`
- Required headers: `access-token` and `client-id`
- Market Quote: up to 1000 instruments in one request, one request per second
- Option Chain rate limit: one unique request every three seconds

Primary references:

- [DhanHQ Market Quote documentation](https://dhanhq.co/docs/v2/market-quote/)
- [DhanHQ v2 API rate limits](https://dhanhq.co/docs/v2/)

Config placeholders are already in `.env.example`:

```text
LIVE_OPTION_CHAIN_PROVIDER=dhan
LIVE_MARKET_QUOTE_MIN_INTERVAL_SECONDS=1.0
LIVE_OPTION_CHAIN_MIN_INTERVAL_SECONDS=3.0
LIVE_SYMBOLS=RELIANCE,SBIN,INFY,HDFCBANK,TCS,NIFTY,BANKNIFTY
LIVE_POLL_INTERVAL_SECONDS=180
LIVE_MARKET_START_IST=09:00
LIVE_MARKET_END_IST=16:00
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
```

The worker intentionally does nothing when Dhan credentials are blank. This allows the same
deployment to run historical EOD analytics before broker credentials are added.

## Implementation Shape

Current split:

1. `app/sources/dhan.py`: broker API client. It only knows Dhan request/response shapes.
2. Dhan detailed scrip master CSV: maps NSE symbols to Dhan `UnderlyingScrip` and `UnderlyingSeg`.
3. `scripts/live_snapshot_worker.py`: polls all active symbols for basic quotes, normalizes the response, and writes latest payloads to Redis.
4. API read path:
   - `GET /api/live`: latest basic quote payloads for all cached active symbols.
   - `GET /api/live/{symbol}`: latest basic quote payload for one symbol.
   - `GET /api/symbol/{symbol}/term-structure`: cached EOD term-structure history with the
     latest live IV/factor/slope row overlaid from Redis when available.
   - `GET /api/live/{symbol}/option-chain`: cached full option chain, fetching Dhan on demand if missing.
   - `POST /api/admin/live-quotes`: manual basic quote refresh.
   - `POST /api/admin/live-snapshot`: manual option-chain snapshot refresh for selected symbols.

That architecture keeps broker credentials out of request handlers, respects Dhan's rate limits,
and lets email/error alerting reuse the existing `error_log` global handler path.

## Unit Policy

Dhan reports option-chain `implied_volatility` as a percent-style number in its sample response.
Before combining it with historical DB values, normalize it to decimal volatility:

```text
11.94 from Dhan -> 0.1194 in DB/API analytics
```

Prices, OI, volume, bid/ask, and Greeks can be stored as reported after numeric validation.

Historical contract IV/Greeks are still recalculated internally from NSE bhavcopy using
Black-Scholes-Merton because historical Dhan Greeks are not available from the live snapshot API.
Dhan Greeks are consumed only for live snapshots and should not be mixed into historical
backtests unless a broker-provided historical chain source is added.

The live quote payload overlays NSE option-chain IV analytics onto the latest EOD baseline:

- `iv_30`, `iv_60`, and `iv_90` are recomputed from live ATM IV across the 30/60/90 expiry hints.
- `fwdv_3060`, `fwdfct_3060`, `fev_30`, and `iv_slope_3060` reuse the same formula helpers as the
  EOD pipeline.
- EOD values are preserved as `eod_iv_30`, `eod_iv_60`, `eod_iv_90`, `eod_fwdv_3060`,
  `eod_fwdfct_3060`, `eod_fev_30`, and `eod_iv_slope_3060` whenever live values are present.
- Source markers such as `iv_term_structure_source='nse:option-chain-v3'` and
  `forward_analytics_source='nse:option-chain-v3'` identify live overlays.

DhanHQ's Expired Options Data API is the planned external validator for historical IV. It exposes
expired option OHLC, IV, OI, volume, strike, and spot for up to the last five years, with up to
30 days per request. Use it as a sample validator against our internally recalculated historical
IV after `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` are configured.

## Operation

Start/restart the worker:

```bash
docker compose up -d --build worker
```

Trigger one manual basic quote refresh after adding credentials:

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
worker remains alive but does not poll Dhan. On-demand API calls still require Dhan credentials.

## Remaining Work

- Add email alerting in the existing global/error-log path.
- Add broker-neutral live-provider abstraction if a second provider is introduced.
