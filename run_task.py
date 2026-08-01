#!/usr/bin/env python3
"""run_task.py — orquestra UMA run: workspace -> agent -> verify -> results.tsv.

Uso:
    python3 run_task.py tasks/task_01
    python3 run_task.py tasks/task_01 --repeat 3
    python3 run_task.py --all --repeat 3
    python3 run_task.py --all --suite sealed        # só creditar generalização

Done = verify.py sai com 0. Nada mais. O que o agent diz não conta.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent import BACKEND, MODEL, run_agent  # noqa: E402

ROOT = Path(__file__).parent.resolve()
# evolve.py roda o runner a partir de uma sandbox (agent.py patchado) mas grava
# no log canônico: a candidata precisa entrar no MESMO results.tsv do baseline,
# senão score.py --ab não tem os dois lados para comparar.
RESULTS = Path(os.environ.get("HARNESS_RESULTS", ROOT / "results.tsv"))
# Tasks moram no repo; a sandbox copia só o genome.
TASKS_ROOT = Path(os.environ.get("HARNESS_TASKS_ROOT", ROOT)).resolve()
HEADER = [
    "timestamp",
    "harness_version",
    "backend",
    "model",
    "suite",
    "task_id",
    "success",
    "seconds",
    "tokens",
    "cost_usd",
    "turns",
    "notes",
]


def harness_version() -> str:
    f = ROOT / "harness_version.txt"
    return f.read_text().strip() if f.exists() else "v0"


def append_result(row: dict) -> None:
    if not RESULTS.exists():
        RESULTS.write_text("\t".join(HEADER) + "\n")
    line = "\t".join(str(row.get(c, "")).replace("\t", " ").replace("\n", " ") for c in HEADER)
    with RESULTS.open("a") as fh:
        fh.write(line + "\n")


def run_once(task_dir: Path, suite: str, keep: bool) -> bool:
    task_id = task_dir.name
    prompt = (task_dir / "prompt.md").read_text()
    verify = task_dir / "verify.py"
    if not verify.exists():
        raise SystemExit(f"{task_id}: falta verify.py — sem verificador não existe done")

    ws = Path(tempfile.mkdtemp(prefix=f"harness_{task_id}_"))
    try:
        fixtures = task_dir / "fixtures"
        if fixtures.is_dir():
            shutil.copytree(fixtures, ws, dirs_exist_ok=True)

        res = run_agent(prompt, ws)

        # verify roda NO workspace, sempre — mesmo se o agent falhou/estourou turns.
        try:
            v = subprocess.run(
                [sys.executable, str(verify)], cwd=ws, capture_output=True, text=True, timeout=120
            )
            success = 1 if v.returncode == 0 else 0
            vnote = "" if success else f"verify:{(v.stdout + v.stderr).strip()[-160:]}"
        except subprocess.TimeoutExpired:
            success, vnote = 0, "verify:timeout"

        notes = "; ".join(n for n in (res.notes, vnote) if n)
        append_result(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "harness_version": harness_version(),
                "backend": BACKEND,
                "model": MODEL,
                "suite": suite,
                "task_id": task_id,
                "success": success,
                "seconds": f"{res.seconds:.1f}",
                "tokens": res.tokens,
                "cost_usd": f"{res.cost_usd:.4f}",
                "turns": res.turns,
                "notes": notes,
            }
        )
        mark = "PASS" if success else "FAIL"
        print(f"[{mark}] {task_id}  {res.seconds:.1f}s  {res.tokens}tok  ${res.cost_usd:.4f}  {notes}")
        if keep:
            print(f"       workspace: {ws}")
        return bool(success)
    finally:
        if not keep:
            shutil.rmtree(ws, ignore_errors=True)


def discover(suite: str) -> list[Path]:
    base = TASKS_ROOT / "tasks" if suite == "fixed" else TASKS_ROOT / "benchmarks" / suite
    return sorted(p for p in base.glob("task_*") if (p / "prompt.md").exists())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", help="pasta da task (ex: tasks/task_01)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--suite", default="fixed", help="fixed | sealed")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="não apagar o workspace")
    a = ap.parse_args()

    tasks = discover(a.suite) if a.all else [Path(a.task).resolve()] if a.task else []
    if not tasks:
        ap.error("informe uma task ou --all")

    passed = total = 0
    for _ in range(a.repeat):
        for t in tasks:
            total += 1
            passed += run_once(t, a.suite, a.keep)

    print(f"\n{harness_version()} [{a.suite}] {passed}/{total} = {passed / total:.0%}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
