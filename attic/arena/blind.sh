#!/usr/bin/env bash
# Anonimiza as variantes de uma geracao para a banca.
# Copia real (nao symlink) — symlink vaza o alvo em `ls -la`, defeito da gen1.
# uso: blind.sh <gen>
set -eu

ARENA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="${1:-1}"
G="$ARENA/gen$GEN"

rm -rf "$G/_blind"
mkdir -p "$G/_blind"

python3 - "$G" <<'PY'
import json, os, random, shutil, sys
g = sys.argv[1]
vs = sorted(d for d in os.listdir(g) if d.startswith('v') and os.path.isdir(os.path.join(g, d)))
labels = [chr(ord('A') + i) for i in range(len(vs))]
random.shuffle(vs)
m = dict(zip(labels, vs))
json.dump(m, open(os.path.join(g, '_blind_map.json'), 'w'), indent=1)

RUNNER = {'result.json', 'stderr.log', 'exit_code', 'started_at', 'ended_at', 'INHERITED.md'}
for label, v in m.items():
    dst = os.path.join(g, '_blind', label)
    shutil.copytree(
        os.path.join(g, v), dst,
        ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.venv', 'node_modules'),
    )
    for f in RUNNER:                      # tira rastro do runner do que a banca ve
        p = os.path.join(dst, f)
        if os.path.exists(p):
            os.remove(p)
print(f"{len(m)} variantes anonimizadas em {g}/_blind (copia real)")
PY

chmod -R a-w "$G/_blind"   # banca nao modifica artefato
echo "mapa: $G/_blind_map.json (nao mostrar a nenhum juiz)"
