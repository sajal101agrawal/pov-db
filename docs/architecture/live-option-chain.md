# Live Option-Chain Architecture

Last updated: 2026-05-20.

## Current State

Historical EOD option-chain data is implemented through NSE/Samco bhavcopy ingestion.
Live option-chain ingestion is not yet scheduled into `live_snapshot`; the API endpoint
`/api/live/{symbol}` only reads Redis/live snapshots if another process has written them.

## Recommended Provider

DhanHQ v2 is the cleanest fit for the next phase because its option-chain API returns the full
chain for an underlying and expiry, including OI, Greeks, volume, top bid/ask, last price, and IV.
The official docs define:

- `POST https://api.dhan.co/v2/optionchain/expirylist`
- `POST https://api.dhan.co/v2/optionchain`
- Required headers: `access-token` and `client-id`
- Rate limit: one unique option-chain request every 3 seconds

Config placeholders are already in `.env.example`:

```text
LIVE_OPTION_CHAIN_PROVIDER=dhan
LIVE_OPTION_CHAIN_MIN_INTERVAL_SECONDS=3.0
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
```

## Implementation Shape

Use this split:

1. `app/sources/dhan.py`: broker API client. It only knows Dhan request/response shapes.
2. Instrument master table or mapping file: maps NSE symbols to Dhan `UnderlyingScrip` and `UnderlyingSeg`.
3. Live updater job: polls the selected symbols/expiries, normalizes the response, writes latest payload to Redis, and periodically persists snapshots to `live_snapshot`.
4. API read path: `/api/live/{symbol}` and `/api/symbol/{symbol}` stay read-only and should not call Dhan directly.

That architecture keeps broker credentials out of request handlers, respects Dhan’s rate limit, and lets email/error alerting reuse the existing `error_log` global handler path.

## Unit Policy

Dhan reports option-chain `implied_volatility` as a percent-style number in its sample response.
Before combining it with historical DB values, normalize it to decimal volatility:

```text
11.94 from Dhan -> 0.1194 in DB/API analytics
```

Prices, OI, volume, bid/ask, and Greeks can be stored as reported after numeric validation.

## Remaining Work

- Add a `live_instruments` table or JSON config for Dhan security IDs.
- Add a Redis writer service for polling selected symbols.
- Add validation that live IV units are normalized before writing.
- Add alerting in the existing global/error-log path.
