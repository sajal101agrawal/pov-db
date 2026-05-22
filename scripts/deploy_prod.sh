#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo ".env is required for production deploy" >&2
  exit 1
fi

docker compose -p pov-db -f docker-compose.prod.yml up -d postgres redis
docker compose -p pov-db -f docker-compose.prod.yml run --rm api python scripts/apply_schema_updates.py
docker compose -p pov-db -f docker-compose.prod.yml up -d --build api worker

if [[ "${INSTALL_CRON:-1}" == "1" ]]; then
  COMPOSE_ARGS="-p pov-db -f docker-compose.prod.yml" scripts/install_daily_etl_cron.sh
fi

docker compose -p pov-db -f docker-compose.prod.yml ps
