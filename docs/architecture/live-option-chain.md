# Live Market Data Architecture

Last updated: 2026-06-25.

## Current State

Historical EOD option-chain data is implemented through NSE/Samco bhavcopy ingestion. Live market
data runs as the `worker` container in Docker Compose. Production runs Kite as the primary live
provider, while local fallbacks continue to work without a valid Kite session.

There are two live paths:

- All-symbol quote and summary refresh: the worker polls the configured quote provider for all
  active F&O symbols during the IST market window. The quote payload is enriched with the configured
  option-summary provider and is written to Redis plus `live_symbol_metrics`.
- Symbol-specific full chain: `GET /api/live/{symbol}/option-chain` fetches the configured
  option-chain provider on demand and caches the chain payload. Successful option-chain fetches are
  also inserted into `live_snapshot` for audit/history.

## Recommended Provider

The recommended production live stack is:

- Kite Market Quote REST for underlying and option quote snapshots. Kite quote snapshots do not
  include IV, so the service calculates live IV from option prices using Black-Scholes.
- Kite nearest-ATM CE/PE quotes for each 30/60/90 expiry target. This keeps the poll efficient while
  producing live ATM strike, strike volume, IV30/60/90, slope, and forward factors.
- Kite full-chain snapshot on demand for `/api/live/{symbol}/option-chain`.
- NSE `option-chain-v3` fallback for option summaries and full-chain snapshots.
- Yahoo fallback for underlying quote snapshots when Kite quote refresh fails.
- Dhan remains available as an alternate provider.

Kite WebSocket is still the better long-term path for tick-level underlying prices because one
connection supports up to 3000 instruments and a single API key supports up to 3 WebSocket
connections. This implementation uses REST quote snapshots first because it can calculate the
required live IV analytics without adding a binary WebSocket ingestion loop.

Config placeholders are already in `.env.example`:

```text
LIVE_QUOTE_PROVIDER=kite
LIVE_OPTION_SUMMARY_PROVIDER=kite
LIVE_OPTION_CHAIN_PROVIDER=kite
LIVE_DHAN_OPTION_SUMMARY_BATCH_SIZE=25
LIVE_DHAN_OPTION_SUMMARY_BATCH_DELAY_SECONDS=3.0
LIVE_KITE_QUOTE_BATCH_SIZE=500
LIVE_KITE_QUOTE_BATCH_DELAY_SECONDS=1.1
LIVE_MARKET_QUOTE_MIN_INTERVAL_SECONDS=1.0
LIVE_OPTION_CHAIN_MIN_INTERVAL_SECONDS=3.0
LIVE_SYMBOLS=all
LIVE_POLL_INTERVAL_SECONDS=180
LIVE_MARKET_START_IST=09:00
LIVE_MARKET_END_IST=16:00
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
DHAN_PIN=
DHAN_TOTP_SECRET=
DHAN_TOKEN_REFRESH_MARGIN_SECONDS=1800
KITE_CLIENT_ID=
KITE_API_KEY=
KITE_API_SECRET=
KITE_ACCESS_TOKEN=
KITE_REQUEST_TOKEN=
KITE_AUTO_REFRESH_ENABLED=true
KITE_TOKEN_REFRESH_TIME_IST=06:05
```

For Kite live data:

- `KITE_API_KEY` and `KITE_API_SECRET` are required.
- `KITE_ACCESS_TOKEN` can be used as a manual static token for the current day.
- `KITE_REQUEST_TOKEN` can be exchanged once for a daily access token.
- `GET /api/admin/kite/login-url` returns the login URL.
- `POST /api/admin/kite/session` exchanges and stores a token from the JSON body
  `{"request_token": "..."}`.
- The 06:05 IST worker hook can only exchange a fresh `KITE_REQUEST_TOKEN`; Kite does not support
  unattended access-token generation from only API key/secret for retail sessions.

`LIVE_SYMBOLS=all` (or blank) means all active symbols from `symbol_universe`; a comma-separated
list limits live polling to those symbols.

## Implementation Shape

Current split:

1. `app/sources/kite.py`: Kite session exchange, quote, instrument dump, and quote normalization.
2. `app/sources/dhan.py`: Dhan token generation, quote, option-chain, instrument mapping, and
   normalization helpers.
3. `app/sources/yahoo.py`: quote fallback for live underlying prices.
4. `app/sources/nse_option_chain.py`: NSE option-chain summary and full-chain fallback client.
5. `scripts/live_snapshot_worker.py`: polls all active symbols for quote and option-summary data,
   normalizes the response, and writes latest payloads to Redis plus PostgreSQL.
6. API read path:
   - `GET /api/live`: latest basic quote payloads for all cached active symbols.
   - `GET /api/live/{symbol}`: latest basic quote payload for one symbol.
   - `GET /api/all-dashboard`: scanner rows overlaid with live current price, volume, IV, slope,
     and forward factors before live numeric filters are applied.
   - `GET /api/symbol/{symbol}` and `/history`: detail rows overlaid with the latest live payload.
   - `GET /api/symbol/{symbol}/term-structure`: cached EOD term-structure history with the
     latest live IV/factor/slope row overlaid from Redis/Postgres when available.
   - `GET /api/symbol/{symbol}/volatility-cone`: cached EOD cone with current IV/DTE overlaid from
     Redis/Postgres when available.
   - `GET /api/live/{symbol}/option-chain`: cached full option chain, fetching the configured
     provider on demand if missing.
   - `POST /api/admin/live-quotes`: manual basic quote refresh.
   - `POST /api/admin/live-snapshot`: manual option-chain snapshot refresh for selected symbols.

The API read path checks Redis first and then `live_symbol_metrics`, which prevents data from
falling back to stale EOD rows when Redis expires after market close.

## Unit Policy

NSE reports option-chain `impliedVolatility` as a percent-style number. Before combining it with
historical DB values, normalize it to decimal volatility:

```text
11.94 from NSE -> 0.1194 in DB/API analytics
```

Prices, OI, volume, and bid/ask can be stored as reported after numeric validation.

Historical contract IV/Greeks are still recalculated internally from NSE bhavcopy using
Black-Scholes-Merton. Kite live IV is calculated from quote prices. Dhan full-chain snapshots include
Greeks; Kite and NSE fallback snapshots do not currently include live Greeks.

The live quote payload overlays option-chain IV analytics onto the latest EOD baseline:

- `iv_30/60/90`, `call_iv_30/60/90`, and `put_iv_30/60/90` are recomputed from live ATM IV across
  the 30/60/90 expiry hints. Call and put legs remain separate.
- `fwdv_3060`, `fwdfct_3060` (average), `call_fwdfct_3060`, `put_fwdfct_3060`, `fev_30`, and
  `iv_slope_3060` reuse the same formula helpers as the EOD pipeline.
- The average IV/factor path requires both call and put IV for a live tenor. A missing side leaves
  `fwdfct_3060` null while preserving the available `call_fwdfct_3060` or `put_fwdfct_3060`.
- EOD values are preserved as `eod_iv_30`, `eod_iv_60`, `eod_iv_90`, `eod_fwdv_3060`,
  matching `eod_*` keys whenever live values are present.
- Source markers such as `iv_term_structure_source='kite:quote:calculated-iv'`,
  `iv_term_structure_source='dhan:optionchain'`, or
  `iv_term_structure_source='nse:option-chain-v3'` identify live overlays.

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

The default market window is `09:00` to `16:00` IST on weekdays. Outside this window the worker
remains alive but does not poll live quotes. API reads still return the latest row from
`live_symbol_metrics` after Redis expires.

## Remaining Work

- Add Kite WebSocket quote ingestion for lower-latency underlying price ticks.
- Add email alerting in the existing global/error-log path.
