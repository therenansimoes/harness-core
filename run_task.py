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
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent import BACKEND, MODEL, run_agent  # noqa: E402
import isolation  # noqa: E402
import kpi  # noqa: E402

ROOT = Path(__file__).parent.resolve()
# evolve.py roda o runner a partir de uma sandbox (agent.py patchado) mas grava
# no log canônico: a candidata precisa entrar no MESMO results.tsv do baseline,
# senão score.py --ab não tem os dois lados para comparar.
RESULTS = Path(os.environ.get("HARNESS_RESULTS", ROOT / "results.tsv"))
# Tasks moram no repo; a sandbox copia só o genome.
TASKS_ROOT = Path(os.environ.get("HARNESS_TASKS_ROOT", ROOT)).resolve()
TRACE_ROOT = Path(os.environ.get("HARNESS_TRACE_ROOT", ROOT / "runs"))
TRACE_KEEP_RUNS = 50
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
    # UMA coluna com JSON compacto ({"nome": valor}) — KPI novo no alvo não
    # muda o schema do TSV. Linha antiga sem a coluna: os leitores zipam pelo
    # header do arquivo (score.load, experiment.collect_result), então some
    # como campo vazio em vez de quebrar.
    "kpis",
]


def harness_version() -> str:
    f = ROOT / "harness_version.txt"
    return f.read_text().strip() if f.exists() else "v0"


def hash_test_files(ws: Path) -> dict:
    """SHA-256 de todo test_*.py sob ws, recursivo, chave = path relativo.
    Usado para detectar se o agent editou o exame em vez de resolvê-lo."""
    hashes = {}
    for root, _dirs, files in os.walk(ws):
        for fn in files:
            if fn.startswith("test_") and fn.endswith(".py"):
                p = Path(root) / fn
                hashes[str(p.relative_to(ws))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def append_result(row: dict) -> None:
    if not RESULTS.exists():
        RESULTS.write_text("\t".join(HEADER) + "\n")
    line = "\t".join(str(row.get(c, "")).replace("\t", " ").replace("\n", " ") for c in HEADER)
    with RESULTS.open("a") as fh:
        fh.write(line + "\n")


def gc_traces() -> None:
    """Mantém as TRACE_KEEP_RUNS runs mais recentes em runs/; apaga o resto.
    Nunca apaga um run_id citado em algum verdict.json (persona pode ter
    citado trace.jsonl:N daquela run — apagar quebraria a auditoria)."""
    if not TRACE_ROOT.is_dir():
        return
    run_dirs = sorted(
        (p for p in TRACE_ROOT.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if len(run_dirs) <= TRACE_KEEP_RUNS:
        return
    verdicts_dir = ROOT / "judges" / "verdicts"
    verdict_text = ""
    if verdicts_dir.is_dir():
        verdict_text = "\n".join(vf.read_text() for vf in verdicts_dir.rglob("*.json"))
    for p in run_dirs[TRACE_KEEP_RUNS:]:
        if p.name in verdict_text:
            continue
        shutil.rmtree(p, ignore_errors=True)


def run_once(task_dir: Path, suite: str, keep: bool, isolation_mode: str = "tmpdir") -> bool:
    task_id = task_dir.name
    prompt = (task_dir / "prompt.md").read_text()
    verify = task_dir / "verify.py"
    if not verify.exists():
        raise SystemExit(f"{task_id}: falta verify.py — sem verificador não existe done")

    ws = Path(tempfile.mkdtemp(prefix=f"harness_{task_id}_"))
    try:
        fixtures = task_dir / "fixtures"
        if fixtures.is_dir():
            # fixtures/ nunca deveria ter .venv/venv (setup.sh reclona limpo),
            # mas se algum provisionamento antigo/manual deixou um venv
            # (shebang aponta pro python que gerou, não pro do workspace),
            # copiá-lo faz verify.py achar um interpretador quebrado sem
            # pytest. Defesa em profundidade: nunca copiar isso pro workspace.
            shutil.copytree(
                fixtures, ws, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".venv", "venv"),
            )

        tests_before = hash_test_files(ws)
        res = run_agent(prompt, ws)
        tests_after = hash_test_files(ws)
        tampered = tests_after != tests_before

        # verify roda NO workspace, sempre — mesmo se o agent falhou/estourou turns.
        # --isolation docker: só o verify roda dentro do container (--network
        # none, sem acesso ao host); o agent acima já rodou fora, editando ws
        # diretamente. Ver isolation.py para a limitação dessa divisão.
        try:
            if isolation_mode == "docker":
                isolation.ensure_image()
                v = isolation.run_verify_in_container(ws, verify)
            else:
                v = subprocess.run(
                    [sys.executable, str(verify)], cwd=ws, capture_output=True, text=True, timeout=120
                )
            success = 1 if v.returncode == 0 else 0
            vnote = "" if success else f"verify:{(v.stdout + v.stderr).strip()[-160:]}"
        except subprocess.TimeoutExpired:
            success, vnote = 0, "verify:timeout"

        # KPI do alvo: roda no workspace DEPOIS do verify (o estado medido é o
        # que o agent entregou). Alvo sem .harness/kpi.toml => {}.
        kpis = kpi.collect(ws)

        trace_token = f"trace:{res.trace_path}" if res.trace_path else ""
        if tampered:
            # o agent editou o test_*.py em vez de resolver a tarefa — hard fail,
            # independente do returncode do verify.
            success = 0
            notes = "; ".join(n for n in ("tamper:test_file_modified", trace_token) if n)
        else:
            notes = "; ".join(n for n in (res.notes, vnote, trace_token) if n)
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
                "kpis": kpi.to_json(kpis),
            }
        )
        mark = "PASS" if success else "FAIL"
        kpi_note = f"  kpis={kpi.to_json(kpis)}" if kpis else ""
        print(f"[{mark}] {task_id}  {res.seconds:.1f}s  {res.tokens}tok  ${res.cost_usd:.4f}  {notes}{kpi_note}")
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
    ap.add_argument(
        "--isolation", default="tmpdir", choices=["tmpdir", "docker"],
        help="tmpdir (default, atual) | docker: verify roda em container --rm --network none",
    )
    a = ap.parse_args()

    if a.isolation == "docker" and not isolation.docker_available():
        ap.error("--isolation docker: docker indisponível (binário ausente ou daemon parado)")

    gc_traces()

    tasks = discover(a.suite) if a.all else [Path(a.task).resolve()] if a.task else []
    if not tasks:
        ap.error("informe uma task ou --all")

    passed = total = 0
    for _ in range(a.repeat):
        for t in tasks:
            total += 1
            passed += run_once(t, a.suite, a.keep, a.isolation)

    print(f"\n{harness_version()} [{a.suite}] {passed}/{total} = {passed / total:.0%}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
