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

Run one daily EOD update after the NSE bhavcopy is published:

```bash
python scripts/daily_update.py --date YYYY-MM-DD
```

Install the daily ETL cron:

```bash
scripts/install_daily_etl_cron.sh
```

Export a validated local DB for RDS restore:

```bash
scripts/export_postgres_dump.sh
```

For live data, add `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` to `.env`. The worker polls
basic Dhan quotes for all active F&O symbols during the IST market window. Full option chains
are fetched and cached on demand through `GET /api/live/{symbol}/option-chain`.

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
