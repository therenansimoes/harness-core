#!/usr/bin/env python3
"""verify task_02 — summarize.py roda e bate com o golden recomputado. exit 0 = pass."""

import csv
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

if not Path("summarize.py").exists():
    print("summarize.py não existe")
    sys.exit(1)

with Path("vendas.csv").open(encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh))
por_produto: dict[str, float] = defaultdict(float)
for r in rows:
    por_produto[r["produto"]] += float(r["valor"])

golden = "\n".join(
    [
        f"LINHAS: {len(rows)}",
        f"TOTAL: {sum(float(r['valor']) for r in rows):.2f}",
        f"REGIOES: {','.join(sorted({r['regiao'] for r in rows}))}",
        f"TOP_PRODUTO: {max(por_produto, key=lambda k: por_produto[k])}",
        f"MEDIA_QTD: {sum(int(r['quantidade']) for r in rows) / len(rows):.2f}",
    ]
)

try:
    proc = subprocess.run(
        [sys.executable, "summarize.py"], capture_output=True, text=True, timeout=30
    )
except subprocess.TimeoutExpired:
    print("summarize.py: timeout")
    sys.exit(1)

if proc.returncode != 0:
    print(f"summarize.py exit {proc.returncode}: {proc.stderr.strip()[-160:]}")
    sys.exit(1)

got = proc.stdout.strip()
if got != golden:
    print(f"saída difere. esperado={golden!r} obtido={got!r}")
    sys.exit(1)
print("ok")
