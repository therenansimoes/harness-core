#!/usr/bin/env bash
# Gate deterministico da GEN4 — sem LLM, sem chamada paga. PASS exige (a)-(e);
# qualquer falha e reportada com motivo e o script sai != 0.
#
#   (a) pytest -q verde na copia do candidato
#   (b) NOTES.md existe com secao de hipotese (grep hipotese|hypothesis + prev)
#   (c) run.sh existe, executavel, exit 0 em <=120s
#   (d) diff nao-vazio vs seed (mudou algo de verdade)
#   (e) judges/_sealed/ e arquivos PRE-EXISTENTES em tests/ intocados
#       (tests NOVOS sao permitidos)
#
# uso: milestone_gate.sh <candidate_dir> [seed_dir]
set -u

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAND_IN="${1:?uso: milestone_gate.sh <candidate_dir> [seed_dir]}"
SEED_IN="${2:-$ARENA/seed}"

CAND="$(cd "$CAND_IN" 2>/dev/null && pwd)" || { echo "FAIL  $CAND_IN"; echo "  - diretorio de candidato inexistente: $CAND_IN"; exit 1; }
SEED="$(cd "$SEED_IN" 2>/dev/null && pwd)" || { echo "FAIL  $CAND_IN"; echo "  - diretorio de seed inexistente: $SEED_IN"; exit 1; }

REASONS=()
PASS=1
fail() { PASS=0; REASONS+=("$1"); }

# ---------- (a) pytest verde ----------
if [ ! -d "$CAND/tests" ]; then
  fail "(a) sem diretorio tests/ em $CAND"
else
  ( cd "$CAND" && timeout 300 python3 -m pytest tests/ -q ) > "$CAND/.gate_pytest.log" 2>&1
  RC=$?
  if [ "$RC" -ne 0 ]; then
    fail "(a) pytest -q nao verde (exit=$RC) — ver $CAND/.gate_pytest.log"
  fi
fi

# ---------- (b) NOTES.md com hipotese e ganho previsto ----------
if [ ! -f "$CAND/NOTES.md" ]; then
  fail "(b) NOTES.md ausente"
else
  if ! grep -Eqi 'hip[oó]tese|hypothesis' "$CAND/NOTES.md"; then
    fail "(b) NOTES.md sem secao de hipotese (grep hipotese|hypothesis vazio)"
  fi
  if ! grep -Eqi 'prev' "$CAND/NOTES.md"; then
    fail "(b) NOTES.md sem ganho previsto (grep prev vazio)"
  fi
fi

# ---------- (c) run.sh executavel e verde em <=120s ----------
if [ ! -f "$CAND/run.sh" ]; then
  fail "(c) run.sh ausente"
elif [ ! -x "$CAND/run.sh" ]; then
  fail "(c) run.sh existe mas nao e executavel (chmod +x)"
else
  ( cd "$CAND" && timeout 120 ./run.sh ) > "$CAND/.gate_run.log" 2>&1
  RC=$?
  if [ "$RC" -eq 124 ]; then
    fail "(c) run.sh nao terminou em 120s (morto pelo timeout)"
  elif [ "$RC" -ne 0 ]; then
    fail "(c) run.sh saiu com exit=$RC — ver $CAND/.gate_run.log"
  fi
fi

# ---------- (d) e (e): diff estrutural vs seed (sem depender de .git — o seed
# nao tem .git de proposito, ver build_seed.sh) ----------
PYOUT="$(python3 - "$SEED" "$CAND" <<'PY'
import os, sys, filecmp

seed, cand = sys.argv[1], sys.argv[2]

# artefatos do runner/gate, gerados em tempo de execucao: nao contam como "mudanca real"
IGNORE_NAMES = {
    '.git', '__pycache__', '.pytest_cache', 'node_modules',
    'result.json', 'stderr.log', 'exit_code', 'started_at', 'ended_at',
    'phase1.json', 'phase1.err', 'phase1_checkpoint',
    '.gate_pytest.log', '.gate_run.log', 'INHERITED.md',
}

def walk_files(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_NAMES]
        for f in filenames:
            if f in IGNORE_NAMES:
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root)
            out[rel] = full
    return out

seed_files = walk_files(seed)
cand_files = walk_files(cand)

diffs = []
for rel, sfull in seed_files.items():
    cfull = cand_files.get(rel)
    if cfull is None:
        diffs.append(('removed', rel))
    elif not filecmp.cmp(sfull, cfull, shallow=False):
        diffs.append(('changed', rel))
for rel in cand_files:
    if rel not in seed_files:
        diffs.append(('added', rel))

print(f"GATE_D_DIFF_COUNT={len(diffs)}")
if not diffs:
    print("GATE_D_FAIL=diff vazio vs seed -- nenhuma mudanca real")

sep = os.sep
protected = []
for kind, rel in diffs:
    if rel == f"judges{sep}_sealed" or rel.startswith(f"judges{sep}_sealed{sep}"):
        protected.append(f"{kind}:{rel}")
    elif rel.startswith(f"tests{sep}") and kind in ("changed", "removed"):
        protected.append(f"{kind}:{rel}")

if protected:
    print("GATE_E_FAIL=" + "; ".join(protected[:10]))

print("GATE_D_SAMPLE=" + "; ".join(f"{k}:{r}" for k, r in diffs[:8]))
PY
)"

if echo "$PYOUT" | grep -q '^GATE_D_FAIL='; then
  fail "(d) $(echo "$PYOUT" | sed -n 's/^GATE_D_FAIL=//p')"
fi
if echo "$PYOUT" | grep -q '^GATE_E_FAIL='; then
  fail "(e) arquivo protegido alterado: $(echo "$PYOUT" | sed -n 's/^GATE_E_FAIL=//p')"
fi

echo "-- evidencia (d): $(echo "$PYOUT" | grep '^GATE_D_DIFF_COUNT=')"
echo "-- amostra: $(echo "$PYOUT" | sed -n 's/^GATE_D_SAMPLE=//p')"

if [ "$PASS" -eq 1 ]; then
  echo "PASS  $(basename "$CAND")"
  exit 0
else
  echo "FAIL  $(basename "$CAND")"
  for r in "${REASONS[@]}"; do echo "  - $r"; done
  exit 1
fi
