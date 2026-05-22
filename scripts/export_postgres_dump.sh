#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OUT="${OUT:-data/pov-prod.dump}"
DB_URL="${DATABASE_URL:-postgresql://pov:pov@localhost:5433/pov}"

mkdir -p "$(dirname "$OUT")"
if command -v pg_dump >/dev/null 2>&1; then
  pg_dump "$DB_URL" --format=custom --file="$OUT"
else
  docker compose exec -T postgres pg_dump -U pov -d pov --format=custom >"$OUT"
fi

echo "wrote $OUT"
