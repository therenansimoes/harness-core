#!/usr/bin/env bash
# d6_autopilot.sh — aceite oficial do D6. Custa $0: roda com HARNESS_MOCK_AGENT=1,
# nenhuma chamada de API/rede em nenhum passo (fila e auto-evolução).
#
# O que este aceite prova é o LOOP DE CONTROLE, não a qualidade do modelo:
# 20 minutos sem intervenção, tetos que valem, escrita confinada à raiz do repo
# e o repositório do jeito que estava (fora dos paths esperados).
#
#   bash tests/acceptance/d6_autopilot.sh
#   AP_MINUTES=2 bash tests/acceptance/d6_autopilot.sh   # janela curta p/ CI
#
# Exit 0 = todas as asserções passaram.

set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

AP_MINUTES="${AP_MINUTES:-20}"
HARD_TIMEOUT=$(python3 -c "print(int(float('$AP_MINUTES')*60+60))")
DEMO="$ROOT/.harness_demo"
PROJ_ROOT="$DEMO/projects"
WORK="$DEMO/work"
BACKUP="$DEMO/_backup"
NAME="demo_ap"

FAILED=0
ok()   { printf '  [ok]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAILED=1; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  (cond: $2)"; fi; }

echo "== D6 autopilot — aceite (mock, \$0) · janela ${AP_MINUTES}min"

# ---------------------------------------------------------------- preparação
rm -rf "$DEMO"
mkdir -p "$PROJ_ROOT" "$WORK" "$BACKUP"

# Arquivos do repo que o ciclo de auto-evolução legitimamente escreve. Guardar
# e devolver no fim: um aceite não pode deixar o log canônico alterado.
for f in results.tsv harness_version.txt agent.py evolution/decisions.jsonl; do
    [ -f "$f" ] && { mkdir -p "$BACKUP/$(dirname "$f")"; cp -p "$f" "$BACKUP/$f"; }
done
git status --porcelain > "$DEMO/git_before.txt"

export HARNESS_PROJECTS_ROOT="$PROJ_ROOT"
export HARNESS_MOCK_AGENT=1

python3 project.py add "$NAME" --path "$WORK" >/dev/null

cat > "$DEMO/prompt_ok.md" <<'EOF'
Escreva o artefato do workspace.
EOF
cat > "$DEMO/prompt_fail.md" <<'EOF'
Escreva o artefato do workspace.
MOCK_NOTES: error_max_turns
EOF
cat > "$DEMO/verify_ok.py" <<'EOF'
import sys
from pathlib import Path
sys.exit(0 if Path("AGENT_OUTPUT.txt").exists() else 1)
EOF
cat > "$DEMO/verify_fail.py" <<'EOF'
import sys
print("unidade reprovada de proposito (aceite D6)")
sys.exit(1)
EOF

# 6 unidades, 2 falhando com nota forjada `error_max_turns` — é esse sinal que
# o catálogo classifica e que faz o step_self ter o que propor.
for i in 1 2; do
    python3 project.py queue "$NAME" add "fail-$i" \
        --prompt "$DEMO/prompt_fail.md" --verify "$DEMO/verify_fail.py" >/dev/null
done
for i in 1 2 3 4; do
    python3 project.py queue "$NAME" add "ok-$i" \
        --prompt "$DEMO/prompt_ok.md" --verify "$DEMO/verify_ok.py" >/dev/null
done

RESULTS="$PROJ_ROOT/$NAME/results.tsv"
BEFORE_LINES=$( [ -f "$RESULTS" ] && wc -l < "$RESULTS" || echo 0 )

# sentinela t0: tudo criado depois dela é "arquivo novo" para a asserção de
# escrita confinada.
T0="$DEMO/t0"
touch "$T0"

# ------------------------------------------------------------------- execução
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
echo "-- rodando autopilot (hard timeout ${HARD_TIMEOUT}s)"
set +e
if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "$HARD_TIMEOUT" python3 autopilot.py --minutes "$AP_MINUTES" \
        --budget 0 --self-every 3 --project "$NAME" > "$DEMO/autopilot.out" 2>&1
else
    python3 autopilot.py --minutes "$AP_MINUTES" \
        --budget 0 --self-every 3 --project "$NAME" > "$DEMO/autopilot.out" 2>&1
fi
EXIT=$?
set -e
tail -3 "$DEMO/autopilot.out" | sed 's/^/     | /'

# ------------------------------------------------------------------ asserções
echo "-- asserções"
check "exit ∈ {0,3} (fila vazia ou deadline), obtido: $EXIT" '[ "$EXIT" = "0" ] || [ "$EXIT" = "3" ]'

AFTER_LINES=$( [ -f "$RESULTS" ] && wc -l < "$RESULTS" || echo 0 )
GREW=$((AFTER_LINES - BEFORE_LINES))
check "results.tsv do projeto cresceu >=5 linhas (cresceu $GREW)" '[ "$GREW" -ge 5 ]'

LOG="$(ls -t evolution/autopilot/*.jsonl 2>/dev/null | head -1)"
check "log jsonl da sessão existe" '[ -n "$LOG" ] && [ -f "$LOG" ]'
if [ -n "$LOG" ]; then
    N_SELF=$(grep -c '"kind": "self"' "$LOG" || true)
    N_PROJ=$(grep -c '"kind": "project"' "$LOG" || true)
    check "log tem >=1 evento kind=self (tem $N_SELF)" '[ "$N_SELF" -ge 1 ]'
    check "log tem >=5 eventos kind=project (tem $N_PROJ)" '[ "$N_PROJ" -ge 5 ]'
    check "log tem o resumo final (finally sempre escreve)" 'grep -q "\"kind\": \"summary\"" "$LOG"'
fi

# escrita confinada: nada novo em /tmp, no TMPDIR do sistema nem em $HOME.
STRAY=$(find /tmp /var/folders "$HOME" -maxdepth 2 -newer "$T0" \
        \( -name '*harness*' -o -name 'ap-*' \) 2>/dev/null \
        | grep -v "^$ROOT" | head -5 || true)
check "nenhum arquivo harness novo fora de ROOT" '[ -z "$STRAY" ]'
[ -n "$STRAY" ] && printf '     ! %s\n' $STRAY

check "workspaces ficaram sob ROOT/.harness_ws" '[ -d "$ROOT/.harness_ws" ] || [ "$GREW" -eq 0 ]'

# ------------------------------------------------------------------- limpeza
# devolve o que o ciclo de auto-evolução escreveu no repo ANTES de julgar o
# git status — o que sobrar depois disso é sujeira de verdade.
for f in results.tsv harness_version.txt agent.py evolution/decisions.jsonl; do
    [ -f "$BACKUP/$f" ] && cp -p "$BACKUP/$f" "$f"
done
rm -f evolution/proposals/auto-*.md evolution/decisions/auto-*.md
rm -rf evolution/sandboxes/auto-* evolution/rollbacks

git status --porcelain > "$DEMO/git_after.txt"
NEW_DIRTY=$(comm -13 <(sort "$DEMO/git_before.txt") <(sort "$DEMO/git_after.txt") || true)
check "git status sem paths inesperados" '[ -z "$NEW_DIRTY" ]'
[ -n "$NEW_DIRTY" ] && printf '     ! %s\n' "$NEW_DIRTY"

echo
if [ "$FAILED" = "0" ]; then
    echo "== D6 ACEITE: PASSOU (exit=$EXIT, +$GREW runs de fila, $N_SELF ciclo(s) self)"
else
    echo "== D6 ACEITE: FALHOU — veja [FAIL] acima e $DEMO/autopilot.out"
fi
exit "$FAILED"
