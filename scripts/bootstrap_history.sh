#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

YEARS="${YEARS:-5}"
END_DATE="${END_DATE:-$(date +%F)}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created .env from .env.example"
fi

mkdir -p data
docker compose up -d postgres redis
docker compose build api worker
docker compose run --rm api python scripts/apply_schema_updates.py
docker compose run --rm api \
  python scripts/initialize_market_data.py \
  --years "$YEARS" \
  --end "$END_DATE" \
  $EXTRA_ARGS

docker compose run --rm api python scripts/validate_database.py --output data/validation_database.json
docker compose exec -T redis redis-cli FLUSHDB >/dev/null
docker compose up -d api worker

echo "bootstrap complete through ${END_DATE} (${YEARS} years)"
