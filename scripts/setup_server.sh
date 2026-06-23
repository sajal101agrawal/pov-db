#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required but was not found on PATH" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is required but is not available" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created .env from .env.example"
fi

mkdir -p data
docker compose up -d --build postgres redis
docker compose build api worker
docker compose run --rm api python scripts/apply_schema_updates.py
docker compose up -d api worker

echo "waiting for API health..."
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8001/api/health >/dev/null 2>&1; then
    echo "setup complete: API is healthy at http://localhost:8001"
    exit 0
  fi
  sleep 2
done

echo "API did not become healthy within 120 seconds" >&2
docker compose ps
exit 1
