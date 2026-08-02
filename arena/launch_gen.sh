#!/usr/bin/env bash
# Dispara uma geração inteira em paralelo, cada variante numa sessão tmux com prazo duro.
# uso: launch_gen.sh <gen> [n_variants] [model] [deadline_s]
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="${1:-1}"
N="${2:-5}"
MODEL="${3:-sonnet}"
DEADLINE="${4:-300}"

# Snapshot do repo antes: qualquer escrita fora de arena/ vira violação detectável.
cd "$ARENA/.." || exit 1
git status --porcelain > "$ARENA/gen$GEN/_repo_before.txt" 2>/dev/null

for i in $(seq 1 "$N"); do
  S="g${GEN}v${i}"
  tmux kill-session -t "$S" 2>/dev/null
  tmux new-session -d -s "$S" \
    "bash '$ARENA/run_variant.sh' '$GEN' 'v$i' '$MODEL' '$DEADLINE'"
  echo "lancado: $S -> arena/gen$GEN/v$i"
done

echo
echo "acompanhar:  tmux ls"
echo "assistir:    tmux attach -t g${GEN}v1   (ctrl-b d para sair)"
echo "prazo:       ${DEADLINE}s"
