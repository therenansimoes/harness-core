#!/usr/bin/env python3
"""verify task_s01 — transform.py roda e resumo.txt bate com o golden recomputado.

exit 0 = pass.
"""
import json
import subprocess
import sys
from pathlib import Path

if not Path("transform.py").exists():
    print("transform.py não existe")
    sys.exit(1)

if not Path("pedidos.json").exists():
    print("pedidos.json não existe (fixture ausente)")
    sys.exit(1)

pedidos = json.loads(Path("pedidos.json").read_text(encoding="utf-8"))

golden_rows = []
for p in pedidos:
    total = sum(item["quantidade"] * item["preco"] for item in p["itens"])
    golden_rows.append((p["id"], p["cliente"], round(total, 2)))

golden_rows.sort(key=lambda r: (-r[2], r[0]))
golden = "\n".join(f"{i}|{c}|{t:.2f}" for i, c, t in golden_rows)

try:
    proc = subprocess.run(
        [sys.executable, "transform.py"], capture_output=True, text=True, timeout=30
    )
except subprocess.TimeoutExpired:
    print("transform.py: timeout")
    sys.exit(1)

if proc.returncode != 0:
    print(f"transform.py exit {proc.returncode}: {proc.stderr.strip()[-160:]}")
    sys.exit(1)

out = Path("resumo.txt")
if not out.exists():
    print("resumo.txt não foi criado")
    sys.exit(1)

got = out.read_text(encoding="utf-8").strip("\n")
if got != golden:
    print(f"resumo.txt difere. esperado={golden!r} obtido={got!r}")
    sys.exit(1)
print("ok")
