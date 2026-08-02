#!/usr/bin/env bash
# Roda uma variante da arena com prazo duro.
# uso: run_variant.sh <gen> <variant> [model] [deadline_s]
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="$1"
VAR="$2"
MODEL="${3:-sonnet}"
DEADLINE="${4:-300}"

DIR="$ARENA/gen$GEN/$VAR"
mkdir -p "$DIR"
cd "$DIR" || exit 1

PROMPT="$(cat "$ARENA/BRIEFING.md")"

# Herança: a partir da gen 2, feedback e leaderboard das gerações anteriores.
INHERIT="$DIR/INHERITED.md"
if [ -f "$INHERIT" ]; then
  PROMPT="$PROMPT

---

# HERANÇA DAS GERAÇÕES ANTERIORES

$(cat "$INHERIT")"
fi

date +%s > started_at

timeout -s TERM "$DEADLINE" \
  claude -p "$PROMPT" \
    --model "$MODEL" \
    --output-format json \
    --permission-mode bypassPermissions \
    --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
    > result.json 2> stderr.log

echo $? > exit_code
date +%s > ended_at
