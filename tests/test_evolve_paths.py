#!/usr/bin/env python3
"""Testa os caminhos do evolve.py sem gastar API: merge, discard e crédito sealed.

Stuba APENAS `run_suite` (a parte cara). Todo o resto do ciclo é o código real:
parse do proposal, sandbox, patch, juiz do score.py, decisão de crédito com
held-out, decision em markdown, promoção (ou não) do genome e graph.

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


def row(v, task, sec, tok, cost, i, suite="fixed", success=1):
    return (
        f"2026-08-01T0{i % 10}:00:0{i % 10}+00:00\t{v}\tcli\tm\t{suite}\t{task}\t{success}\t"
        f"{sec}\t{tok}\t{cost:.4f}\t3\t"
    )


def run_case(pid, fixed_cand, expect_rc, sealed=None, sealed_base=None,
             expect_credited=None) -> list[str]:
    """Um ciclo completo com a suite stubada.

    fixed_cand / sealed: (seconds, tokens, cost, success) da candidata.
    sealed is None  -> repo sem benchmarks/sealed (merge sai sem crédito).
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"evolve_{pid}_"))
    try:
        for f in ("agent.py", "run_task.py", "score.py", "graph.py", "evolve.py"):
            shutil.copy2(REPO / f, tmp / f)
        shutil.copytree(REPO / "tasks", tmp / "tasks")
        if sealed is not None:
            shutil.copytree(REPO / "benchmarks" / "sealed", tmp / "benchmarks" / "sealed")
        (tmp / "harness_version.txt").write_text("vA\n")
        (tmp / "evolution" / "proposals").mkdir(parents=True)
        (tmp / "evolution" / "decisions").mkdir(parents=True)
        prop = tmp / "evolution" / "proposals" / f"{pid}.md"
        prop.write_text(PROPOSAL.format(pid=pid))

        lines = [HEADER] + [
            row("vA", f"task_0{i % 3 + 1}", 24.0, 2000, 0.0600, i) for i in range(9)
        ]
        if sealed_base:
            s, t, c, ok = sealed_base
            lines += [
                row("vA", f"task_s0{i % 2 + 1}", s, t, c, i, "sealed", ok) for i in range(4)
            ]
        (tmp / "results.tsv").write_text("\n".join(lines) + "\n")

        os.environ["HARNESS_GRAPH"] = str(tmp / "critique.db")
        sys.path.insert(0, str(tmp))
        for m in ("evolve", "score", "graph"):
            sys.modules.pop(m, None)
        import evolve  # noqa: E402
        import graph  # noqa: E402

        calls = []

        def fake_suite(sandbox, suite, repeat):
            calls.append(suite)
            spec = fixed_cand if suite == "fixed" else sealed
            if spec is None:
                return 0
            s, t, c, ok = spec
            n, pref = (9, "task_0") if suite == "fixed" else (4, "task_s0")
            mod = 3 if suite == "fixed" else 2
            with (tmp / "results.tsv").open("a") as fh:
                for i in range(n):
                    fh.write(row("vB", f"{pref}{i % mod + 1}", s, t, c, i, suite, ok) + "\n")
            return 0

        evolve.run_suite = fake_suite
        rc = evolve.cycle(prop, repeat=3, suite="fixed", force=False)

        merged = expect_rc == 0
        fails = []
        if rc != expect_rc:
            fails.append(f"exit esperado {expect_rc}, obtido {rc}")

        version = (tmp / "harness_version.txt").read_text().strip()
        patched = "MAX_TURNS = 11" in (tmp / "agent.py").read_text()
        if merged:
            if version != "vB":
                fails.append(f"version deveria virar vB, é {version}")
            if not patched:
                fails.append("genome não foi promovido")
        else:
            if version != "vA":
                fails.append(f"DISCARD sujou o baseline: version={version}")
            if patched:
                fails.append("DISCARD promoveu o genome — baseline contaminado")

        dec = tmp / "evolution" / "decisions" / f"{pid}.md"
        if not dec.exists():
            fails.append("decision não foi escrita")
        else:
            txt = dec.read_text()
            if ("MERGE" if merged else "DISCARD") not in txt:
                fails.append("decision não registra o outcome certo")
            if expect_credited is True and "Confirmado em **sealed**" not in txt:
                fails.append("decision não registra o crédito em sealed")
            if expect_credited is False and "NÃO creditado" not in txt and merged:
                fails.append("merge sem held-out não foi marcado como não creditado")

        if sealed is None and "sealed" in calls:
            fails.append("rodou sealed sem suite sealed existir")
        if sealed is not None and expect_rc != 2 and "sealed" not in calls:
            fails.append("candidata aprovada na fixed não foi levada ao held-out")

        decs = graph.recent_decisions(5)
        want = "merge" if merged else "discard"
        if not decs or decs[0]["outcome"] != want:
            fails.append(f"graph não registrou {want}: {decs[:1]}")

        sys.path.remove(str(tmp))
        return fails
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    bom = (18.0, 1400, 0.0420, 1)   # -25% tempo, -30% custo
    ruim = (26.0, 2200, 0.0660, 1)  # mais lenta e mais cara
    cases = [
        # (nome, candidata fixed, exit, sealed candidata, sealed baseline, creditado?)
        ("merge_sem_sealed", bom, 0, None, None, False),
        ("discard_na_fixed", ruim, 1, None, None, None),
        ("merge_creditado", bom, 0, (20.0, 1500, 0.0450, 1), (24.0, 2000, 0.0600, 1), True),
        # ganho na fixed, mas a held-out quebra: success cai de 100% para 0%
        ("sealed_reprova", bom, 1, (20.0, 1500, 0.0450, 0), (24.0, 2000, 0.0600, 1), None),
    ]
    bad = 0
    for pid, cand, rc, sealed, sbase, cred in cases:
        fails = run_case(pid, cand, rc, sealed, sbase, cred)
        if fails:
            bad += 1
            print(f"FALHOU {pid}:\n  - " + "\n  - ".join(fails))
        else:
            print(f"OK {pid}: exit {rc}, baseline e graph coerentes.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
