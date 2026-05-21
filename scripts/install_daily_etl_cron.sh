#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRON_TIME="${CRON_TIME:-30 22 * * 1-5}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/data/daily_etl.log}"
COMPOSE_ARGS="${COMPOSE_ARGS:-}"
PY_DATE='$(date +\%F)'
DOCKER_COMPOSE="docker compose $COMPOSE_ARGS"
CRON_CMD="cd $ROOT_DIR && /usr/bin/env bash -lc '$DOCKER_COMPOSE run --rm api python scripts/apply_schema_updates.py >> $LOG_FILE 2>&1 && $DOCKER_COMPOSE run --rm api python scripts/daily_update.py --date $PY_DATE >> $LOG_FILE 2>&1 && $DOCKER_COMPOSE run --rm api python scripts/validate_database.py --output data/validation_database.json >> $LOG_FILE 2>&1 && $DOCKER_COMPOSE exec -T redis redis-cli FLUSHDB >> $LOG_FILE 2>&1'"
CRON_LINE="$CRON_TIME $CRON_CMD # pov-db-daily-etl"

mkdir -p "$ROOT_DIR/data"

tmp="$(mktemp)"
if crontab -l >"$tmp" 2>/dev/null; then
  grep -v '# pov-db-daily-etl$' "$tmp" >"${tmp}.new" || true
else
  : >"${tmp}.new"
fi
printf '%s\n' "$CRON_LINE" >>"${tmp}.new"
crontab "${tmp}.new"
rm -f "$tmp" "${tmp}.new"

echo "installed cron:"
echo "$CRON_LINE"
