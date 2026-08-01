#!/usr/bin/env python3
"""score.py — lê results.tsv e agrega por (harness_version, suite).

    python3 score.py               # resumo por versão
    python3 score.py --by-task     # abre por task (onde a falha mora)
    python3 score.py --ab v0 v0.1  # compara duas versões + regra de promoção

Não decide nada sozinho: imprime números e diz se os gates passam.
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).parent / "results.tsv"


def load() -> list[dict]:
    if not RESULTS.exists():
        raise SystemExit("results.tsv não existe — rode run_task.py primeiro")
    lines = RESULTS.read_text().strip().splitlines()
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def agg(rows: list[dict]) -> dict:
    n = len(rows)
    succ = sum(int(r["success"]) for r in rows)
    secs = [float(r["seconds"]) for r in rows]
    cost = sum(float(r["cost_usd"] or 0) for r in rows)
    toks = sum(int(r["tokens"] or 0) for r in rows)
    return {
        "n": n,
        "pass": succ,
        "rate": succ / n if n else 0.0,
        "med_s": statistics.median(secs) if secs else 0.0,
        "cost": cost,
        "tokens": toks,
        # efficiency do PLAN: success por unidade de custo
        "eff": (succ / cost) if cost else 0.0,
    }


def fmt(label: str, a: dict) -> str:
    return (
        f"{label:<18} {a['pass']:>3}/{a['n']:<3} = {a['rate']:>5.0%}  "
        f"med {a['med_s']:>5.1f}s  {a['tokens']:>7}tok  ${a['cost']:>7.4f}  eff {a['eff']:>5.1f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--by-task", action="store_true")
    ap.add_argument("--ab", nargs=2, metavar=("A", "B"))
    a = ap.parse_args()
    rows = load()

    if a.ab:
        va, vb = a.ab
        ra = [r for r in rows if r["harness_version"] == va]
        rb = [r for r in rows if r["harness_version"] == vb]
        if not ra or not rb:
            raise SystemExit(f"faltam runs: {va}={len(ra)} {vb}={len(rb)}")
        A, B = agg(ra), agg(rb)
        print(fmt(f"A {va}", A))
        print(fmt(f"B {vb}", B))

        gates = [
            ("success não caiu", B["rate"] >= A["rate"]),
            ("n suficiente (>=3 por lado)", A["n"] >= 3 and B["n"] >= 3),
            ("custo sob controle (<=1.5x A ou success subiu)", B["cost"] <= A["cost"] * 1.5 or B["rate"] > A["rate"]),
            ("ganho real (rate subiu ou custo caiu)", B["rate"] > A["rate"] or B["cost"] < A["cost"]),
        ]
        print()
        for name, ok in gates:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        allok = all(ok for _, ok in gates)
        print(f"\n=> {'MERGE candidato' if allok else 'DISCARD'} — confirme em sealed antes de creditar.")
        print("   Registre a decisão em evolution/decisions/.")
        return 0 if allok else 1

    key = (lambda r: (r["harness_version"], r["suite"], r["task_id"])) if a.by_task else (
        lambda r: (r["harness_version"], r["suite"])
    )
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    for k in sorted(groups):
        print(fmt(" ".join(k), agg(groups[k])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
