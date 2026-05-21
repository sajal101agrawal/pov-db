#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OUT="${OUT:-data/pov-prod.dump}"
DB_URL="${DATABASE_URL:-postgresql://pov:pov@localhost:5433/pov}"

mkdir -p "$(dirname "$OUT")"
pg_dump "$DB_URL" --format=custom --file="$OUT"

echo "wrote $OUT"
