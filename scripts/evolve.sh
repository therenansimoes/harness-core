#!/usr/bin/env bash
# evolve.sh — roda 1 ciclo de auto-melhoria do harness e loga em data/evolve.log
set -euo pipefail

BACKEND="${BACKEND:-mock}"   # backend real custa dinheiro; mock é o default seguro
MODEL="${MODEL:-}"           # vazio = default do backend
MAX_CYCLES="${MAX_CYCLES:-1}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

LOG_FILE="$REPO_DIR/data/evolve.log"
mkdir -p "$REPO_DIR/data"

CMD=(uv run --extra deepagents harness improve --cycles "$MAX_CYCLES" --backend "$BACKEND")
if [[ -n "$MODEL" ]]; then
  CMD+=(--model "$MODEL")
fi

{
  echo "=== $(date '+%Y-%m-%dT%H:%M:%S%z') evolve start (backend=$BACKEND cycles=$MAX_CYCLES model=${MODEL:-default})"
} >> "$LOG_FILE"

set +e
"${CMD[@]}" >> "$LOG_FILE" 2>&1
rc=$?
set -e

echo "=== $(date '+%Y-%m-%dT%H:%M:%S%z') evolve end (exit=$rc)" >> "$LOG_FILE"
exit "$rc"
