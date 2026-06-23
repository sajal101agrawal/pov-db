#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
  echo ".env is required for production deploy" >&2
  exit 1
fi

docker compose -p pov-db -f docker-compose.prod.yml up -d postgres redis
docker compose -p pov-db -f docker-compose.prod.yml build api worker
docker compose -p pov-db -f docker-compose.prod.yml run --rm api python scripts/apply_schema_updates.py

if [[ "${RUN_CORPORATE_ACTION_BACKFILL:-0}" == "1" ]]; then
  docker compose -p pov-db -f docker-compose.prod.yml run --rm api \
    python scripts/backfill_corporate_action_metrics.py --execute
else
  echo "corporate-action backfill not run; legacy RV/VRP remains API-disabled until remediation"
  echo "preview: docker compose -p pov-db -f docker-compose.prod.yml run --rm api python scripts/backfill_corporate_action_metrics.py"
fi

docker compose -p pov-db -f docker-compose.prod.yml exec -T redis redis-cli FLUSHDB >/dev/null
docker compose -p pov-db -f docker-compose.prod.yml up -d api worker

if [[ "${INSTALL_CRON:-1}" == "1" ]]; then
  COMPOSE_ARGS="-p pov-db -f docker-compose.prod.yml" scripts/install_daily_etl_cron.sh
fi

docker compose -p pov-db -f docker-compose.prod.yml ps
