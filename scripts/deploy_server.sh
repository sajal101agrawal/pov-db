#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BOOTSTRAP="${BOOTSTRAP:-auto}"
INSTALL_CRON="${INSTALL_CRON:-1}"
YEARS="${YEARS:-5}"
END_DATE="${END_DATE:-$(date +%F)}"

scripts/setup_server.sh

if [[ "$BOOTSTRAP" == "1" ]]; then
  YEARS="$YEARS" END_DATE="$END_DATE" scripts/bootstrap_history.sh
elif [[ "$BOOTSTRAP" == "auto" ]]; then
  existing="$(docker compose exec -T postgres psql -U pov -d pov -Atc "SELECT COUNT(*) FROM options_historical;" | tr -d '[:space:]')"
  if [[ "${existing:-0}" == "0" ]]; then
    YEARS="$YEARS" END_DATE="$END_DATE" scripts/bootstrap_history.sh
  else
    echo "historical data already exists (${existing} option rows); skipping bootstrap"
  fi
fi

if [[ "$INSTALL_CRON" == "1" ]]; then
  scripts/install_daily_etl_cron.sh
fi

docker compose up -d --build api worker
echo "deploy complete"
