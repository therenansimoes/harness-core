#!/usr/bin/env bash
# Dispara os 4 candidatos da GEN4 em paralelo, cada um numa sessao tmux com
# prazo duro. Adaptado de arena/launch_gen.sh (gen1-3), trocando N variantes
# genericas por v1..v4 fixos (mutacao auto-dirigida: o alvo vem do INHERITED
# de cada um, nao de um numero de geracao/variante generico).
#
# uso: launch_gen4.sh [model] [t_fase1] [t_fase2]
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${1:-claude-opus-5}"
T1="${2:-240}"
T2="${3:-480}"

for N in 1 2 3 4; do
  D="$ARENA/v$N"
  if [ ! -d "$D" ]; then
    echo "ERRO: $D nao existe — rode build_seed.sh antes de lançar." >&2
    exit 1
  fi
done

for N in 1 2 3 4; do
  S="gen4v${N}"
  tmux kill-session -t "$S" 2>/dev/null
  tmux new-session -d -s "$S" \
    "bash '$ARENA/run_candidate.sh' 'v$N' '$MODEL' '$T1' '$T2'"
  echo "lancado: $S -> arena/gen4/v$N"
done

echo
echo "acompanhar:  tmux ls"
echo "assistir:    tmux attach -t gen4v1   (ctrl-b d para sair)"
echo "prazo total: $((T1 + T2))s por candidato (fase1 ${T1}s + fase2 ${T2}s)"
echo "gate:        bash '$ARENA/milestone_gate.sh' '$ARENA/vN'"
