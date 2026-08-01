#!/usr/bin/env python3
"""Testa os caminhos MERGE e DISCARD do evolve.py sem gastar API.

Stuba APENAS `run_suite` (a parte cara). Todo o resto do ciclo é o código real:
parse do proposal, sandbox, patch, juiz do score.py, decision, promoção (ou não)
do genome e gravação no graph.

Um avaliador precisa saber votar dos dois lados — a mesma regra que vale para os
`verify.py` das tasks vale para o loop de evolução.

    python3 tests/test_evolve_paths.py    # exit 0 = passou
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROPOSAL = """+++
id = "{pid}"
from_version = "vA"
to_version = "vB"
hypothesis = "Candidata sintética usada pelo teste do caminho {pid}."

[change]
file = "agent.py"
old = '''MAX_TURNS = 12'''
new = '''MAX_TURNS = 11'''
+++

Proposta sintética do teste.
"""

HEADER = (
    "timestamp\tharness_version\tbackend\tmodel\tsuite\ttask_id\tsuccess\t"
    "seconds\ttokens\tcost_usd\tturns\tnotes"
)


def row(v: str, task: str, sec: float, tok: int, cost: float, i: int) -> str:
    return (
        f"2026-08-01T0{i % 10}:00:00+00:00\t{v}\tcli\tm\tfixed\t{task}\t1\t"
        f"{sec}\t{tok}\t{cost:.4f}\t3\t"
    )


def run_case(pid: str, cand: tuple[float, int, float], expect_rc: int) -> list[str]:
    """Roda um ciclo completo com a suite stubada. Devolve lista de falhas."""
    sec, tok, cost = cand
    tmp = Path(tempfile.mkdtemp(prefix=f"evolve_{pid}_"))
    try:
        for f in ("agent.py", "run_task.py", "score.py", "graph.py", "evolve.py"):
            shutil.copy2(REPO / f, tmp / f)
        shutil.copytree(REPO / "tasks", tmp / "tasks")
        (tmp / "harness_version.txt").write_text("vA\n")
        (tmp / "evolution" / "proposals").mkdir(parents=True)
        (tmp / "evolution" / "decisions").mkdir(parents=True)
        prop = tmp / "evolution" / "proposals" / f"{pid}.md"
        prop.write_text(PROPOSAL.format(pid=pid))

        # baseline vA: 9 runs de referência
        lines = [HEADER] + [
            row("vA", f"task_0{i % 3 + 1}", 24.0, 2000, 0.0600, i) for i in range(9)
        ]
        (tmp / "results.tsv").write_text("\n".join(lines) + "\n")

        os.environ["HARNESS_GRAPH"] = str(tmp / "critique.db")
        sys.path.insert(0, str(tmp))
        for m in ("evolve", "score", "graph"):
            sys.modules.pop(m, None)
        import evolve  # noqa: E402
        import graph  # noqa: E402

        def fake_suite(sandbox, suite, repeat):
            with (tmp / "results.tsv").open("a") as fh:
                for i in range(9):
                    fh.write(row("vB", f"task_0{i % 3 + 1}", sec, tok, cost, i) + "\n")
            return 0

        evolve.run_suite = fake_suite
        rc = evolve.cycle(prop, repeat=3, suite="fixed", force=False)

        merged = expect_rc == 0
        fails = []
        if rc != expect_rc:
            fails.append(f"exit esperado {expect_rc}, obtido {rc}")

        version = (tmp / "harness_version.txt").read_text().strip()
        genome_patched = "MAX_TURNS = 11" in (tmp / "agent.py").read_text()
        if merged:
            if version != "vB":
                fails.append(f"version deveria virar vB, é {version}")
            if not genome_patched:
                fails.append("genome não foi promovido")
        else:
            if version != "vA":
                fails.append(f"DISCARD sujou o baseline: version={version}")
            if genome_patched:
                fails.append("DISCARD promoveu o genome — baseline contaminado")

        dec = tmp / "evolution" / "decisions" / f"{pid}.md"
        if not dec.exists():
            fails.append("decision não foi escrita")
        elif ("MERGE" if merged else "DISCARD") not in dec.read_text():
            fails.append("decision não registra o outcome certo")

        decs = graph.recent_decisions(5)
        want = "merge" if merged else "discard"
        if not decs or decs[0]["outcome"] != want:
            fails.append(f"graph não registrou {want}: {decs[:1]}")
        runs = graph.runs_for_version("vB")
        if len(runs) != 9:
            fails.append(f"graph deveria ter 9 runs de vB, tem {len(runs)}")
        if any(r["proposal_id"] != pid for r in runs):
            fails.append("runs da candidata não ficaram ligadas ao proposal")

        sys.path.remove(str(tmp))
        return fails
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    cases = [
        # (nome, (seconds, tokens, cost) da candidata, exit esperado)
        ("synthetic_merge", (18.0, 1400, 0.0420), 0),    # -25% tempo, -30% custo
        ("synthetic_discard", (26.0, 2200, 0.0660), 1),  # mais lenta e mais cara
    ]
    bad = 0
    for pid, cand, rc in cases:
        fails = run_case(pid, cand, rc)
        if fails:
            bad += 1
            print(f"FALHOU {pid}:\n  - " + "\n  - ".join(fails))
        else:
            print(f"OK {pid}: exit {rc}, baseline e graph coerentes.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
