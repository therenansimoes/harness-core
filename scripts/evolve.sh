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

# dream antes do improve: o && só deixa o sono rodar quando should_dream diz que há dever de sono
{
  uv run python -c "
from harness.triggers.watch import should_dream
from harness.ledger.store import db_path
from datetime import datetime, timezone
import sys
sys.exit(0 if should_dream(db_path(), datetime.now(timezone.utc)) else 1)
" && uv run python -c "
from harness.improve import dream
p = dream.propose_dream()
p and dream.apply_dream(p)
print('dream:', 'aplicado' if p else 'sem material')
" || echo 'dream: sem dever de sono'
} >> "$LOG_FILE" 2>&1

set +e
"${CMD[@]}" >> "$LOG_FILE" 2>&1
rc=$?
set -e

echo "=== $(date '+%Y-%m-%dT%H:%M:%S%z') evolve end (exit=$rc)" >> "$LOG_FILE"
exit "$rc"
