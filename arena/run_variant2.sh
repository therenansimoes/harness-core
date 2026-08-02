#!/usr/bin/env bash
# Runner de duas fases com checkpoint duro.
# Fase 1: prazo curto, unico objetivo = run.sh verde + NOTES.md. Nao morre no meio.
# Fase 2: retoma a MESMA sessao e endurece, com o entregavel ja garantido em disco.
# Isso existe porque na gen2 quatro de cinco foram mortos trabalhando e nao
# entregaram run.sh nem NOTES.md: pedir no prompt nao e mecanismo.
#
# uso: run_variant2.sh <gen> <variant> [model] [t_fase1] [t_fase2]
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="$1"; VAR="$2"
MODEL="${3:-sonnet}"
T1="${4:-100}"
T2="${5:-195}"

DIR="$ARENA/gen$GEN/$VAR"
mkdir -p "$DIR"
cd "$DIR" || exit 1

BRIEF="$(cat "$ARENA/BRIEFING.md")"
INHERIT=""
[ -f "$DIR/INHERITED.md" ] && INHERIT="

---

# HERANÇA

$(cat "$DIR/INHERITED.md")"

date +%s > started_at

# ---------- FASE 1 ----------
P1="$BRIEF$INHERIT

---

# FASE 1 DE 2 — VOCÊ TEM ${T1} SEGUNDOS. SÓ ISTO IMPORTA AGORA.

Objetivo único desta fase: garantir que \`./run.sh\` roda e que \`NOTES.md\` existe.
Você já tem uma base funcional no diretório. NÃO adicione funcionalidade nesta fase.

1. Rode \`./run.sh\` agora e veja o que acontece.
2. Faça a menor mudança necessária para ele rodar limpo do estado atual.
3. Escreva um \`NOTES.md\` curto: o que a base faz, o que você pretende mudar na fase 2.

Quando \`./run.sh\` passar e \`NOTES.md\` existir, PARE e diga 'FASE 1 OK'.
Você será retomado na fase 2 com o tempo restante para melhorar de verdade."

timeout -s TERM "$T1" \
  claude -p "$P1" --model "$MODEL" --output-format json \
    --permission-mode bypassPermissions \
    --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
    > phase1.json 2> phase1.err
echo $? > phase1_exit

SID=$(python3 -c "
import json,sys
try:
    d=json.load(open('phase1.json'))
    if isinstance(d,list): d=d[-1]
    print(d.get('session_id',''))
except Exception: print('')
" 2>/dev/null)

# checkpoint: o entregavel existe?
[ -f run.sh ] && chmod +x run.sh 2>/dev/null
echo "run_sh=$([ -f run.sh ] && echo 1 || echo 0) notes=$([ -f NOTES.md ] && echo 1 || echo 0) sid=${SID:-none}" > phase1_checkpoint

# ---------- FASE 2 ----------
P2="# FASE 2 DE 2 — VOCÊ TEM ${T2} SEGUNDOS E SERÁ MORTO NO PRAZO.

O entregável mínimo já está em disco. A partir daqui, a regra é uma só:
**\`./run.sh\` tem que continuar rodando limpo depois de CADA mudança sua.**

Rode \`./run.sh\` depois de cada alteração. Se quebrar, conserte antes de seguir.
Um harness que não roda vale zero, por mais elegante que seja o código.

Agora melhore de verdade, na ordem de peso do briefing. Atualize o NOTES.md
conforme avança — não deixe para o fim, porque não há fim: você será morto trabalhando."

if [ -n "$SID" ]; then
  timeout -s TERM "$T2" \
    claude -p "$P2" --resume "$SID" --model "$MODEL" --output-format json \
      --permission-mode bypassPermissions \
      --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
      > result.json 2> stderr.log
else
  # fase 1 morreu antes de emitir session_id: segue sem contexto, com o disco como estado
  timeout -s TERM "$T2" \
    claude -p "$BRIEF$INHERIT

$P2" --model "$MODEL" --output-format json \
      --permission-mode bypassPermissions \
      --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch \
      > result.json 2> stderr.log
fi

echo $? > exit_code
[ -f run.sh ] && chmod +x run.sh 2>/dev/null
date +%s > ended_at
