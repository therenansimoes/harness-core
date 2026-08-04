#!/usr/bin/env python3
"""verify task_tb_log_summary — adaptado de terminal-bench-2.0/log-summary-date-ranges.
Origem: HF dataset harborframework/terminal-bench-2.0, task log-summary-date-ranges,
revisão f2e8c75e23add71613117eecc9498f53bcd7e04e (main, 2026-04-24).
fixtures/logs/ é a saída exata (byte a byte, contagens conferidas) do gerador
determinístico environment/log_generator_deterministic.py do task original
(seed por arquivo = ano*10000+mês*100+dia+índice_da_fonte). Os valores
esperados abaixo são os mesmos hardcoded em tests/test_outputs.py
(test_summary_structure_and_counts) do task original — reproduzidos aqui
rodando o gerador localmente e conferindo total ERROR=14160, WARNING=18772,
INFO=56621. exit 0 = pass."""

import csv
import sys
from pathlib import Path

fails: list[str] = []
p = Path("summary.csv")

if not p.exists():
    print("summary.csv não existe")
    sys.exit(1)

EXPECTED_ROWS = [
    ["today", "ERROR", "370"],
    ["today", "WARNING", "463"],
    ["today", "INFO", "1433"],
    ["last_7_days", "ERROR", "2969"],
    ["last_7_days", "WARNING", "4020"],
    ["last_7_days", "INFO", "12203"],
    ["last_30_days", "ERROR", "9594"],
    ["last_30_days", "WARNING", "12847"],
    ["last_30_days", "INFO", "38702"],
    ["month_to_date", "ERROR", "4682"],
    ["month_to_date", "WARNING", "6327"],
    ["month_to_date", "INFO", "19420"],
    ["total", "ERROR", "14160"],
    ["total", "WARNING", "18772"],
    ["total", "INFO", "56621"],
]

with p.open("r", newline="") as f:
    reader = list(csv.reader(f))

if not reader:
    fails.append("CSV vazio")
else:
    header = reader[0]
    if header != ["period", "severity", "count"]:
        fails.append(f"header inesperado: {header}")

    if len(reader) - 1 != len(EXPECTED_ROWS):
        fails.append(f"esperado {len(EXPECTED_ROWS)} linhas, obtido {len(reader) - 1}")

    for expected_row, actual_row in zip(EXPECTED_ROWS, reader[1:], strict=False):
        if actual_row != expected_row:
            fails.append(f"esperado {expected_row}, obtido {actual_row}")

if fails:
    print(" | ".join(fails))
    sys.exit(1)
print("ok")
