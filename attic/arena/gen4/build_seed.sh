#!/usr/bin/env bash
# Monta arena/gen4/seed/ a partir do HEAD do repo e semeia v1..v4 com copias dele.
#
# git archive exclui .git, arquivos nao versionados e (via pathspec) arena/ inteiro
# — cada candidato nao enxerga a arena que o gerou. Duas coisas gitignoradas sao
# copiadas por fora porque sao exigidas em disco para o suite ficar verde:
#   - node_modules/: sem ele "npx playwright --version" falha e o gate de UI
#     (tests/test_ui_gate.py) fica vermelho por ambiente, nao por bug.
#   - runs/harness_task_j_b2b_gp04m5hx/: fixture hardcoded em
#     tests/test_judges.py::test_citacao_com_quebra_de_linha_escapada_nao_veta.
#     Sem isso o pytest do seed intocado JA sai vermelho antes de qualquer
#     candidato tocar em uma linha.
# Provado por execucao (2026-08-02): sem as duas copias, 5+1 testes falham;
# com elas, 142 passed, 0 failed, seed em ~26MB.
#
# uso: build_seed.sh [ref]
set -eu

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$ARENA/../.." && pwd)"
REF="${1:-HEAD}"
SEED="$ARENA/seed"

rm -rf "$SEED"
mkdir -p "$SEED"

cd "$REPO"
git archive "$REF" -- . ':!arena' | tar -x -C "$SEED"

if [ -d "$REPO/node_modules" ]; then
  cp -R "$REPO/node_modules" "$SEED/node_modules"
else
  echo "AVISO: $REPO/node_modules ausente — gate de UI provavelmente ficara vermelho." >&2
fi

FIX="runs/harness_task_j_b2b_gp04m5hx"
if [ -d "$REPO/$FIX" ]; then
  mkdir -p "$SEED/runs"
  cp -R "$REPO/$FIX" "$SEED/$FIX"
else
  echo "AVISO: $REPO/$FIX ausente — test_judges.py::test_citacao_... provavelmente falha." >&2
fi

echo "seed pronto em $SEED"
du -sh "$SEED"

for N in 1 2 3 4; do
  D="$ARENA/v$N"
  rm -rf "$D"
  cp -R "$SEED" "$D"
  echo "candidato v$N semeado em $D ($(du -sh "$D" | cut -f1))"
done
