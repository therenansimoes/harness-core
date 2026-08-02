#!/usr/bin/env bash
# Runner headless do juiz-persona, duas fases separadas por mecanismo (RUBRIC v3).
# Espaço de trabalho: ARENA/_judgespace/gen<N>/<persona>/ (fora de gen<N>, cegueira por mecanismo).
# Fase 1: juiz NAO ve nenhum candidato, monta a propria bancada no bench.
# Entre as fases: snapshot read-only da bancada (copia real, NAO symlink —
# symlink vazou o alvo em `ls -la` na gen 1, ver blind.sh).
# Fase 2: retoma a MESMA sessao (claude --resume) e so ai recebe os caminhos
# de cases/A..E (copias isoladas, caminhos sem gen<N> ni _blind).
#
# uso: run_judge.sh <gen> <persona>
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="${1:-}"
PERSONA="${2:-}"

if [ -z "$GEN" ] || [ -z "$PERSONA" ]; then
  echo "uso: run_judge.sh <gen> <persona>" >&2
  echo "  persona: p1 (web/sites) | p2 (b2b/dados) | p3 (hardware/firmware) | p4 (infra/cli)" >&2
  exit 1
fi

case "$PERSONA" in
  p1|p2|p3|p4) ;;
  *) echo "persona invalida: $PERSONA (use p1..p4)" >&2; exit 1 ;;
esac

G="$ARENA/gen$GEN"
if [ ! -d "$G" ]; then
  echo "geracao nao existe: $G" >&2
  exit 1
fi

RUBRIC="$(cat "$ARENA/RUBRIC.md")"

BENCH="$ARENA/_judgespace/gen$GEN/$PERSONA/bench"
SNAP="$ARENA/_judgespace/gen$GEN/$PERSONA/bench_snapshot"
JOUT="$ARENA/_judgespace/gen$GEN/$PERSONA/out"
mkdir -p "$BENCH" "$JOUT"

T1="${3:-240}"
T2="${4:-600}"

# ---------- FASE 1: bancada as cegas ----------
P1="$RUBRIC

---

# VOCE E O JUIZ $PERSONA DA ARENA — FASE 1 DE 2. VOCE TEM ${T1} SEGUNDOS.

Nesta fase voce NAO recebe nenhum candidato e NAO deve procurar nenhum.
Sua area de interesse (RUBRIC v3): $PERSONA.

Objetivo unico: montar, em \`$BENCH\`, uma bancada de teste real do seu
dominio — codigo que roda, com pelo menos um defeito verdadeiro, e um
criterio objetivo e automatizavel de 'consertado'. Nada de dica plantada em
comentario.

Escreva \`$BENCH/BANCADA.md\` com: o que e o projeto, por que e representativo
do dominio, o que voce espera que um harness competente consiga fazer ali, e
o criterio de sucesso — declarado ANTES de ver qualquer candidato.

Quando a bancada existir, rodar e o defeito estiver confirmado (rode o
criterio de sucesso e mostre que falha no estado atual), PARE e diga
'FASE 1 OK'. Voce sera retomado na fase 2 para julgar candidatos que ainda
nao existem para voce."

cd "$BENCH" || exit 1

date +%s > "$JOUT/started_at"

timeout -s TERM "$T1" \
  claude -p "$P1" --model sonnet --output-format json \
    --permission-mode bypassPermissions \
    --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
    > "$JOUT/phase1.json" 2> "$JOUT/phase1.err"
echo $? > "$JOUT/phase1_exit"

SID=$(python3 -c "
import json,sys
try:
    d=json.load(open('$JOUT/phase1.json'))
    if isinstance(d,list): d=d[-1]
    print(d.get('session_id',''))
except Exception: print('')
" 2>/dev/null)

echo "bancada=$([ -f "$BENCH/BANCADA.md" ] && echo 1 || echo 0) sid=${SID:-none}" > "$JOUT/phase1_checkpoint"

# ---------- validacao da banca cega e monta cases isolados ----------
if [ ! -d "$G/_blind" ]; then
  echo "banca cega ausente: $G/_blind (rode blind.sh $GEN antes)" >&2
  exit 1
fi

CASES="$ARENA/_judgespace/gen$GEN/$PERSONA/cases"
rm -rf "$CASES"
mkdir -p "$CASES"
for LETRA in $(ls -1 "$G/_blind" | sort); do
  cp -R "$G/_blind/$LETRA" "$CASES/$LETRA"
done

# ---------- snapshot read-only da bancada ----------
# copia real, nao symlink (blind.sh ja documentou o vazamento na gen 1)
rm -rf "$SNAP"
mkdir -p "$(dirname "$SNAP")"
cp -R "$BENCH" "$SNAP"
chmod -R a-w "$SNAP"

# ---------- FASE 2: julgamento cego dos candidatos ----------
BLIND_DIRS=$(ls -1 "$CASES" 2>/dev/null | sort | tr '\n' ' ')

P2="# FASE 2 DE 2 — VOCE TEM ${T2} SEGUNDOS. Agora sim, os candidatos.

Sua bancada (snapshot read-only, ja com o defeito confirmado): $SNAP

Os 5 candidatos cegos, um harness por letra: $BLIND_DIRS
Caminho de cada um: cases/<letra>

Para CADA candidato, nesta ordem, sem pular etapa:
1. copia limpa da bancada + copia limpa do candidato (nao reutilize estado entre candidatos)
2. aponte o harness do candidato para a copia da bancada e rode de ponta a ponta
3. execute ANTES de ler o codigo do candidato
4. registre a execucao crua: comando, saida real resumida, exit code, tempo

Pontue pela RUBRIC v3 (secao 'Parte da banca', 70 pts): generalidade/transplante,
trabalho util real produzido, verificacao confiavel, custo e eficiencia.

Emita, para cada candidato, uma ficha no schema JSON da RUBRIC ('Ficha do
juiz'). Ao final, escreva TODAS as fichas como um array JSON em
\`$JOUT/scores.json\`. Nota sem citacao de evidencia e sem registro de
execucao e descartada — nao a emita."

if [ -n "$SID" ]; then
  timeout -s TERM "$T2" \
    claude -p "$P2" --resume "$SID" --model sonnet --output-format json \
      --permission-mode bypassPermissions \
      --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
      > "$JOUT/phase2.json" 2> "$JOUT/phase2.err"
else
  # fase 1 morreu antes de emitir session_id: segue sem contexto, com a bancada em disco como estado
  timeout -s TERM "$T2" \
    claude -p "$RUBRIC

$P2" --model sonnet --output-format json \
      --permission-mode bypassPermissions \
      --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
      > "$JOUT/phase2.json" 2> "$JOUT/phase2.err"
fi

echo $? > "$JOUT/phase2_exit"
date +%s > "$JOUT/ended_at"

echo "juiz $PERSONA geracao $GEN: fase1=$(cat "$JOUT/phase1_exit") fase2=$(cat "$JOUT/phase2_exit") scores=$([ -f "$JOUT/scores.json" ] && echo presente || echo AUSENTE)"
