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

Initialize a fresh server with the last four years of NSE F&O history:

```bash
python scripts/initialize_market_data.py --years 4
```

Run one daily EOD update after the NSE bhavcopy is published:

```bash
python scripts/daily_update.py --date YYYY-MM-DD
```

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
