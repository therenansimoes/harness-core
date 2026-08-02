#!/usr/bin/env bash
# Runner de duas fases da GEN4 — adaptado de arena/run_variant2.sh (gen3), mesma
# razao de ser: pedido no prompt nao e mecanismo, checkpoint duro sim. Fase 1
# curta garante run.sh+NOTES.md em disco (na gen2, 4/5 morreram sem entregar
# nada porque so pediram "escreva cedo" no texto); fase 2 retoma a MESMA sessao
# (--resume) e endurece com o entregavel ja garantido.
#
# uso: run_candidate.sh <variant v1|v2|v3|v4> [model] [t_fase1] [t_fase2]
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAR="${1:?uso: run_candidate.sh <v1|v2|v3|v4> [model] [t_fase1] [t_fase2]}"
MODEL="${2:-claude-opus-5}"
T1="${3:-240}"
T2="${4:-480}"

DIR="$ARENA/$VAR"
[ -d "$DIR" ] || { echo "candidato $DIR nao existe — rode build_seed.sh primeiro" >&2; exit 1; }
cd "$DIR" || exit 1

BRIEF="$(cat "$ARENA/BRIEFING-GEN4.md")"
INHERIT_FILE="$ARENA/INHERITED-$VAR.md"
INHERIT=""
[ -f "$INHERIT_FILE" ] && INHERIT="

---

# HERANCA

$(cat "$INHERIT_FILE")"

date +%s > started_at

# ---------- FASE 1 ----------
P1="$BRIEF$INHERIT

---

# FASE 1 DE 2 — VOCE TEM ${T1} SEGUNDOS. SO ISTO IMPORTA AGORA.

Objetivo unico desta fase: escrever seu diagnostico ranqueado no NOTES.md (top 4,
qual rank voce ataca, ganho previsto) e garantir que \`./run.sh\` roda mostrando
a base intacta. NAO implemente a melhoria ainda.

1. Leia o repo que voce recebeu (e a copia do harness atual, nao um projeto de
   exemplo). Rode \`python3 -m pytest tests/ -q\` para ver que a base parte verde.
2. Escreva o \`NOTES.md\` com seu diagnostico (top 4 alavancas), qual rank ataca
   (esta no seu INHERITED.md) e a hipotese de ganho, ANTES de codar.
3. Crie/ajuste \`./run.sh\` para pelo menos demonstrar a base funcionando.

Quando \`./run.sh\` passar e \`NOTES.md\` tiver a secao de hipotese, PARE e diga
'FASE 1 OK'. Voce sera retomado na fase 2 com o tempo restante para melhorar de
verdade."

timeout -s TERM "$T1" \
  claude -p "$P1" --model "$MODEL" --output-format json \
    --permission-mode bypassPermissions \
    --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Task \
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

[ -f run.sh ] && chmod +x run.sh 2>/dev/null
echo "run_sh=$([ -f run.sh ] && echo 1 || echo 0) notes=$([ -f NOTES.md ] && echo 1 || echo 0) sid=${SID:-none}" > phase1_checkpoint

# ---------- FASE 2 ----------
P2="# FASE 2 DE 2 — VOCE TEM ${T2} SEGUNDOS E SERA MORTO NO PRAZO.

O diagnostico e o entregavel minimo ja estao em disco. A partir daqui:

1. Implemente a melhoria no rank que voce declarou atacar no NOTES.md.
2. **\`./run.sh\` tem que continuar rodando limpo (exit 0) depois de CADA mudanca.**
3. **\`python3 -m pytest tests/ -q\` tem que terminar verde** — nao edite testes
   existentes para faze-lo passar; testes NOVOS que voce escrever sao bem-vindos.
4. Nao toque em nada dentro de \`judges/_sealed/\`.
5. \`./run.sh\` precisa demonstrar a melhoria — nao so a base intacta.

Atualize o NOTES.md conforme avanca, registrando o que verificou executando.
Voce sera morto no prazo, sem aviso — nao ha 'fim', so o relogio."

if [ -n "$SID" ]; then
  timeout -s TERM "$T2" \
    claude -p "$P2" --resume "$SID" --model "$MODEL" --output-format json \
      --permission-mode bypassPermissions \
      --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Task \
      > result.json 2> stderr.log
else
  # fase 1 morreu antes de emitir session_id: segue sem contexto, com o disco como estado
  timeout -s TERM "$T2" \
    claude -p "$BRIEF$INHERIT

$P2" --model "$MODEL" --output-format json \
      --permission-mode bypassPermissions \
      --allowedTools Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch,Task \
      > result.json 2> stderr.log
fi

echo $? > exit_code
[ -f run.sh ] && chmod +x run.sh 2>/dev/null
date +%s > ended_at
