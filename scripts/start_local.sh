#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

if [[ ! -f "$BACKEND/.env" ]]; then
  echo "Create $BACKEND/.env from docs/LOCAL_RUNBOOK.md before starting TradingOS."
  exit 1
fi

cleanup() {
  kill "${API_PID:-}" "${UI_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd "$BACKEND" && uvicorn app.main:app --host 127.0.0.1 --port 8000) &
API_PID=$!
(cd "$FRONTEND" && NUXT_PUBLIC_API_BASE_URL="http://127.0.0.1:8000/api/v1" pnpm dev --host 127.0.0.1 --port 3001) &
UI_PID=$!

echo "TradingOS API: http://127.0.0.1:8000/docs"
echo "TradingOS UI:  http://127.0.0.1:3001"
wait "$API_PID" "$UI_PID"
