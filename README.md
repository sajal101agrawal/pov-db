# POV DB

FastAPI + PostgreSQL/TimescaleDB + Redis service for NSE options analytics.

This repo owns the database layer and nightly analytics pipeline. It stores full NSE option
bhavcopies, equity bars, contract-level IV/Greeks, symbol daily metrics, straddle PnL, and
dashboard-ready aggregate rows.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

API docs are available at `http://localhost:8001/docs`.

Set up a server:

```bash
scripts/setup_server.sh
```

Run the full idempotent deploy flow:

```bash
scripts/deploy_server.sh
```

Initialize a fresh local server with the last five years of NSE F&O history:

```bash
scripts/bootstrap_history.sh
```

Repair/recompute derived analytics after a formula change:

```bash
python scripts/recompute_option_volume.py
python scripts/recompute_symbol_aggregates.py
python scripts/recompute_rsi_metrics.py
python scripts/recompute_straddle_pnl.py
```

Run one daily EOD update after the NSE bhavcopy is published:

```bash
python scripts/daily_update.py --date YYYY-MM-DD
```

Refresh only result/earnings events without running market-data ETL:

```bash
python scripts/update_result_events.py
```

Install the daily ETL cron:

```bash
scripts/install_daily_etl_cron.sh
```

Export a validated local DB for RDS restore:

```bash
scripts/export_postgres_dump.sh
```

For live data, `GET /api/live` returns all active symbols in one response for frontend polling.
It refreshes from Yahoo Finance quotes on cache miss, stores the result in Redis for
`LIVE_CACHE_TTL_SECONDS` seconds (default `300`), and includes the live underlying price. It also
tries NSE `option-chain-v3` for live all-strike CE+PE option volume and overlays that into
`avg_option_volume` with `avg_option_volume_source='nse:option-chain-v3'`; if NSE is unavailable,
the latest local EOD metric is returned with `avg_option_volume_source='symbol_daily_metrics'`.
`live_atm_iv` is included when the NSE option-chain response has usable ATM IV. NSE option-summary
requests are throttled by `LIVE_OPTION_SUMMARY_MIN_INTERVAL_SECONDS` (default `0.25`) with low
concurrency and exponential backoff; persistent `403`/`429` responses stop the current NSE summary
batch and leave the endpoint on EOD fallback data. `GET /api/live/{symbol}` uses the same
cache/refresh path for one symbol. Dhan credentials are only needed for the optional full
option-chain route, `GET /api/live/{symbol}/option-chain`.

## Result Events And Upcoming Earnings

Completed/filed result events come from NSE corporate-event data and are stored in `events`
with `event_type='RESULT'` and `source='nse:event-calendar'`.

Upcoming earnings dates are refreshed from Yahoo Finance's earnings calendar and stored with
`source='yahoo:earnings-calendar'`. Only Yahoo's `Earnings Date` field is used, so dividend
dates and other corporate actions are not stored as result events.

`scripts/daily_update.py` refreshes both sources on every ETL run unless `--skip-events` is
passed. To force only the result-event refresh on a weekend or market holiday, run:

```bash
python scripts/update_result_events.py
```

Useful server command:

```bash
docker compose -p pov-db -f docker-compose.prod.yml run --rm api python scripts/update_result_events.py
docker compose -p pov-db -f docker-compose.prod.yml exec -T redis redis-cli FLUSHDB
```

Options:

```bash
python scripts/update_result_events.py --skip-nse
python scripts/update_result_events.py --symbols RELIANCE,TCS,INFY --skip-nse
```

`GET /api/all-dashboard` returns upcoming result data in each row:

- `result_date`: next upcoming result date.
- `result_event`: `true` when a future result event exists.
- `upcoming_events`: upcoming result events sorted by `event_date`, including `event_date`,
  `event_type`, `description`, and `source`.

Run tests locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Formula Policy

All persisted volatilities are stored as decimals (`0.284` means `28.4%`). API callers can
format percentages at the presentation layer. This avoids the percent/decimal drift present in
the older Django code.

The formula audit is in [docs/calculations/formula-audit.md](docs/calculations/formula-audit.md).

## Current Operations Docs

- [Data sources](docs/architecture/data-sources.md)
- [Database dictionary](docs/architecture/database-dictionary.md)
- [Retries, failures, and validation](docs/operations/retries-errors-validation.md)
- [Current validation report](docs/operations/current-validation-2026-05-20.md)
- [Live option-chain architecture](docs/architecture/live-option-chain.md)
- [AWS deployment notes](docs/operations/aws-deployment.md)
