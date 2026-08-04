#!/usr/bin/env python3
"""verify task_01 — README com seções obrigatórias. exit 0 = pass."""

import re
import sys
from pathlib import Path

fails: list[str] = []
p = Path("README.md")

if not p.exists():
    print("README.md não existe")
    sys.exit(1)

text = p.read_text(encoding="utf-8", errors="replace")

sections = ["## Instalação", "## Uso", "## Opções", "## Exemplo"]
positions = []
for s in sections:
    m = re.search(rf"^{re.escape(s)}\s*$", text, re.MULTILINE)
    if not m:
        fails.append(f"seção ausente: {s}")
    else:
        positions.append(m.start())

if len(positions) == len(sections) and positions != sorted(positions):
    fails.append("seções fora de ordem")

if len(text.splitlines()) < 20:
    fails.append("menos de 20 linhas")

if not re.search(r"```[\s\S]*?csvpeek dados\.csv[\s\S]*?```", text):
    fails.append("falta bloco de código com 'csvpeek dados.csv'")

for flag in ("--rows", "--cols", "--json"):
    if flag not in text:
        fails.append(f"flag não documentada: {flag}")

if len(re.findall(r"```", text)) < 4:
    fails.append("menos de 2 blocos de código")

if fails:
    print(" | ".join(fails))
    sys.exit(1)
print("ok")
