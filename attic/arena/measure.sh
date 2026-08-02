#!/usr/bin/env bash
# Medicao deterministica de uma geracao. Sem LLM. Produz gen<N>/_measured.md
# uso: measure.sh <gen>
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="${1:-1}"
G="$ARENA/gen$GEN"
OUT="$G/_measured.md"

{
echo "# Medicao deterministica — geracao $GEN"
echo

# Violacao de escopo: escreveu fora de arena/?
echo "## Violacao de escopo (escrita fora de arena/)"
cd "$ARENA/.." || exit 1
AFTER=$(git status --porcelain | grep -v '^?? arena/' | grep -v ' arena/' || true)
if [ -n "$AFTER" ]; then
  echo '```'
  echo "$AFTER"
  echo '```'
  echo "**ATENCAO: houve escrita fora de arena/ — investigar qual variante.**"
else
  echo "nenhuma. OK."
fi
echo

for D in "$G"/v*/; do
  V=$(basename "$D")
  echo "## $V"

  # telemetria do CLI
  if [ -s "$D/result.json" ]; then
    python3 - "$D/result.json" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception as e:
    print(f"- result.json ilegivel: {e}"); raise SystemExit
if isinstance(d,list): d=d[-1] if d else {}
u=d.get("usage") or {}
print(f"- custo_usd: {d.get('total_cost_usd')}")
print(f"- turnos: {d.get('num_turns')}")
print(f"- duracao_ms: {d.get('duration_ms')}")
print(f"- subtype: {d.get('subtype')} | is_error: {d.get('is_error')}")
print(f"- tokens_in: {u.get('input_tokens')} | tokens_out: {u.get('output_tokens')}")
PY
  else
    echo "- result.json vazio ou ausente (morto pelo prazo antes de emitir JSON)"
  fi

  S=$(cat "$D/started_at" 2>/dev/null || echo 0)
  E=$(cat "$D/ended_at" 2>/dev/null || echo 0)
  [ "$S" -gt 0 ] && [ "$E" -gt 0 ] && echo "- wall_clock_s: $((E-S))"
  echo "- exit_code: $(cat "$D/exit_code" 2>/dev/null || echo '?')  (124 = morto pelo prazo)"

  # volume de trabalho, excluindo os artefatos do runner
  FILES=$(find "$D" -type f \
      ! -name result.json ! -name stderr.log ! -name exit_code \
      ! -name started_at ! -name ended_at ! -name INHERITED.md \
      ! -path '*/.git/*' ! -path '*/node_modules/*' ! -path '*/__pycache__/*' \
      ! -path '*/.venv/*' 2>/dev/null)
  N=$(echo "$FILES" | grep -c . || echo 0)
  L=$(echo "$FILES" | grep . | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}')
  echo "- arquivos: $N | linhas: ${L:-0}"
  echo "- NOTES.md: $([ -f "$D/NOTES.md" ] && echo presente || echo AUSENTE)"

  echo "- arvore:"
  echo '```'
  (cd "$D" && find . -type f \
      ! -name result.json ! -name stderr.log ! -name exit_code \
      ! -name started_at ! -name ended_at \
      ! -path './.git/*' ! -path './node_modules/*' ! -path '*__pycache__*' \
      ! -path './.venv/*' | sed 's|^\./||' | sort | head -40)
  echo '```'
  echo
done
} > "$OUT"

echo "escrito: $OUT"
