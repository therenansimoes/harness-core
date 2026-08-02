#!/usr/bin/env python3
"""project.py — eixo de ENTREGA multi-projeto (SPEC-MULTIPROJECT FASE 1).

Control-plane em `projects/<nome>/` (repo); data-plane (código real) fora do
repo em `work_path`. Isolamento por unidade de trabalho: copia work_path para
um workspace efêmero, roda o agente lá, só copia o resultado de volta pro
work_path se `verify/<id>.py` sair com 0. Lock por projeto (os.open O_EXCL)
evita dois processos trabalharem no mesmo projeto ao mesmo tempo; pid morto
tem o lock roubado. Fila (`queue.tsv`) é TSV append/rewrite, sem daemon.

    python3 project.py add <nome> --path <abs> [--priority N]
    python3 project.py list
    python3 project.py queue <nome> add "<título>" --prompt f.md --verify v.py
    python3 project.py run [--project N] [--once|--loop K] [--keep]
    python3 project.py status [<nome>]

Overrides de teste (nunca usados em produção):
    HARNESS_PROJECTS_ROOT   raiz alternativa para projects/ (isola tmp_path)
    HARNESS_WS_ROOT         raiz alternativa para os workspaces efêmeros
    HARNESS_MOCK_AGENT=1    troca agent.run_agent por um agente sintético
                            (ver _mock_agent) — NUNCA liga rede/custo. O
                            "agente" mockado lê diretivas de uma linha do
                            prompt (MOCK_TAMPER:/MOCK_FAIL:/MOCK_SLEEP:) para
                            simular tamper, falha e corrida de lock nos
                            testes sem depender do backend real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import graph  # noqa: E402
import kpi  # noqa: E402

ROOT = Path(__file__).parent.resolve()
PROJECTS_ROOT = Path(os.environ.get("HARNESS_PROJECTS_ROOT", ROOT / "projects"))
WS_ROOT = Path(os.environ.get("HARNESS_WS_ROOT", ROOT / ".harness_ws"))

QUEUE_HEADER = ["id", "state", "priority", "created", "claimed_at", "prompt_file", "verify", "notes"]
# Mesmo schema do results.tsv global (run_task.py); cada projeto grava no SEU
# results.tsv, não no da raiz. `kpis` é a ÚNICA coluna que o KPI por projeto
# usa (JSON compacto) — KPI novo no alvo não vira coluna nova aqui.
RESULTS_HEADER = [
    "timestamp", "harness_version", "backend", "model", "suite",
    "task_id", "success", "seconds", "tokens", "cost_usd", "turns", "notes",
    "kpis",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def harness_version() -> str:
    f = ROOT / "harness_version.txt"
    return f.read_text().strip() if f.exists() else "v0"


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ------------------------------------------------------------------- config


def read_config(proj_dir: Path) -> dict:
    import tomllib

    cfg_path = proj_dir / ".harness" / "config.toml"
    if not cfg_path.exists():
        return {}
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    return data.get("project", {})


def write_config(proj_dir: Path, name: str, work_path: str, priority: int, enabled: bool = True) -> None:
    cfg_path = proj_dir / ".harness" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "[project]\n"
        f'name = "{name}"\n'
        f'work_path = "{work_path}"\n'
        f"priority = {int(priority)}\n"
        f"enabled = {'true' if enabled else 'false'}\n"
    )


# --------------------------------------------------------------------- queue


def read_queue(proj_dir: Path) -> list[dict]:
    path = proj_dir / "queue.tsv"
    if not path.exists():
        return []
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    rows = []
    for line in lines[1:]:
        parts = line.split("\t")
        parts += [""] * (len(QUEUE_HEADER) - len(parts))
        rows.append(dict(zip(QUEUE_HEADER, parts)))
    return rows


def write_queue(proj_dir: Path, rows: list[dict]) -> None:
    lines = ["\t".join(QUEUE_HEADER)]
    for r in rows:
        lines.append(
            "\t".join(str(r.get(c, "")).replace("\t", " ").replace("\n", " ") for c in QUEUE_HEADER)
        )
    (proj_dir / "queue.tsv").write_text("\n".join(lines) + "\n")


def _last_activity(proj_dir: Path) -> str:
    """timestamp da última linha de results.tsv — usado como desempate no
    scheduler (starvation-free: projeto nunca rodado vem primeiro, "" < tudo)."""
    path = proj_dir / "results.tsv"
    if not path.exists():
        return ""
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    if len(lines) <= 1:
        return ""
    return lines[-1].split("\t", 1)[0]


def _append_result(proj_dir: Path, row: dict) -> None:
    path = proj_dir / "results.tsv"
    if not path.exists():
        path.write_text("\t".join(RESULTS_HEADER) + "\n")
    line = "\t".join(str(row.get(c, "")).replace("\t", " ").replace("\n", " ") for c in RESULTS_HEADER)
    with path.open("a") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------- lock


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_is_live(proj_dir: Path) -> bool:
    lock_path = proj_dir / ".harness" / "lock"
    if not lock_path.exists():
        return False
    try:
        pid = int(lock_path.read_text().split()[0])
    except (ValueError, IndexError, OSError):
        return False
    return _pid_alive(pid)


def acquire_lock(proj_dir: Path) -> bool:
    """os.open(O_CREAT|O_EXCL) — atômico entre processos. Lock de pid morto
    é roubado (unlink + tenta de novo, uma vez; se perder a corrida, desiste)."""
    lock_dir = proj_dir / ".harness"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "lock"
    for _attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {_now()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            if lock_is_live(proj_dir):
                return False
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    return False


def release_lock(proj_dir: Path) -> None:
    try:
        (proj_dir / ".harness" / "lock").unlink()
    except FileNotFoundError:
        pass


# -------------------------------------------------------------------- agent


def _mock_agent(prompt: str, ws: Path):
    """HARNESS_MOCK_AGENT=1: nunca chama claude/API. Diretivas lidas linha a
    linha do prompt (invisíveis para um agente real, que só vê texto comum):
        MOCK_TAMPER: <path abs>   apenda uma linha nesse arquivo (simula o
                                   agente escapando do ws e editando o
                                   verificador de controle)
        MOCK_FAIL: 1              devolve ok=False
        MOCK_SLEEP: <segundos>    dorme antes de responder (testa corrida de lock)
    Sempre escreve ws/AGENT_OUTPUT.txt como artefato — é o que os verify.py
    de teste checam por padrão."""
    from agent import AgentResult

    t0 = time.time()
    fail = False
    for line in prompt.splitlines():
        line = line.strip()
        if line.startswith("MOCK_TAMPER:"):
            target = Path(line.split(":", 1)[1].strip())
            with target.open("a") as fh:
                fh.write("\n# tampered-by-mock-agent\n")
        elif line.startswith("MOCK_FAIL:"):
            fail = line.split(":", 1)[1].strip() == "1"
        elif line.startswith("MOCK_SLEEP:"):
            time.sleep(float(line.split(":", 1)[1].strip()))
    (ws / "AGENT_OUTPUT.txt").write_text("mock:done\n")
    return AgentResult(
        ok=not fail, seconds=time.time() - t0, tokens=0, cost_usd=0.0,
        turns=1, text="DONE: mock", notes="mock_fail" if fail else "",
    )


def _call_agent(prompt: str, ws: Path):
    if os.environ.get("HARNESS_MOCK_AGENT") == "1":
        return _mock_agent(prompt, ws)
    import agent

    return agent.run_agent(prompt, ws)


# ----------------------------------------------------------------- execução


def _execute(proj_dir: Path, project_name: str, row: dict, all_rows: list[dict], keep: bool) -> bool:
    cfg = read_config(proj_dir)
    work_path = Path(cfg["work_path"]).expanduser()
    unit_id = row["id"]

    row["claimed_at"] = _now()
    write_queue(proj_dir, all_rows)

    ws = WS_ROOT / f"{project_name}_{unit_id}_{uuid.uuid4().hex[:8]}"
    ws.mkdir(parents=True, exist_ok=True)
    if work_path.is_dir():
        shutil.copytree(work_path, ws, dirs_exist_ok=True)

    prompt_path = proj_dir / row["prompt_file"]
    prompt = prompt_path.read_text() if prompt_path.exists() else ""
    verify_path = proj_dir / row["verify"]
    pre_hash = _hash_file(verify_path)

    run_id = f"{project_name}_{unit_id}_{uuid.uuid4().hex[:6]}"
    prev_run_id, prev_trace_root = os.environ.get("HARNESS_RUN_ID"), os.environ.get("HARNESS_TRACE_ROOT")
    os.environ["HARNESS_RUN_ID"] = run_id
    os.environ["HARNESS_TRACE_ROOT"] = str(proj_dir / "runs")
    try:
        res = _call_agent(prompt, ws)
    finally:
        for key, prev in (("HARNESS_RUN_ID", prev_run_id), ("HARNESS_TRACE_ROOT", prev_trace_root)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    post_hash = _hash_file(verify_path)
    tampered = pre_hash != post_hash

    try:
        v = subprocess.run(
            [sys.executable, str(verify_path)], cwd=ws, capture_output=True, text=True, timeout=120,
        )
        verify_ok = v.returncode == 0
        vnote = "" if verify_ok else f"verify:{(v.stdout + v.stderr).strip()[-160:]}"
    except subprocess.TimeoutExpired:
        verify_ok, vnote = False, "verify:timeout"

    if tampered:
        # aceite 5: agent edita verify/<id>.py -> hard fail, independente do
        # returncode do verify (que já não é confiável).
        success = 0
        notes = "; ".join(n for n in ("tamper:verify_modified", res.notes) if n)
    else:
        success = 1 if verify_ok else 0
        notes = "; ".join(n for n in (res.notes, vnote) if n)

    # KPI do alvo medido no ws (o que o agent entregou), antes de qualquer
    # cópia de volta. Projeto sem .harness/kpi.toml no work_path => {}.
    kpis = kpi.collect(ws)

    if success:
        # resultado só é aplicado de volta se verify passou (aceite 7: se
        # falhar, work_path fica intacto — só descartamos o ws).
        shutil.copytree(ws, work_path, dirs_exist_ok=True)

    _append_result(
        proj_dir,
        {
            "timestamp": _now(),
            "harness_version": harness_version(),
            "backend": os.environ.get("HARNESS_BACKEND", "cli"),
            "model": os.environ.get("HARNESS_MODEL", ""),
            "suite": "project",
            "task_id": f"{project_name}/{unit_id}",
            "success": success,
            "seconds": f"{res.seconds:.1f}",
            "tokens": res.tokens,
            "cost_usd": f"{res.cost_usd:.4f}",
            "turns": res.turns,
            "notes": notes,
            "kpis": kpi.to_json(kpis),
        },
    )

    row["state"] = "done" if success else "failed"
    row["notes"] = "; ".join(n for n in (row.get("notes", ""), notes) if n)
    write_queue(proj_dir, all_rows)

    if not keep:
        shutil.rmtree(ws, ignore_errors=True)

    mark = "PASS" if success else "FAIL"
    print(f"[{mark}] {project_name}/{unit_id}  {res.seconds:.1f}s  {notes}")
    return bool(success)


def try_run_one(project_name: str, keep: bool) -> str:
    """Tenta executar 1 unidade pendente do projeto. Devolve 'ran', 'locked'
    (outro processo já tem o lock vivo) ou 'empty' (fila vazia/sem pending)."""
    proj_dir = PROJECTS_ROOT / project_name
    if not proj_dir.is_dir():
        return "missing"
    if not acquire_lock(proj_dir):
        return "locked"
    try:
        rows = read_queue(proj_dir)
        pending = [r for r in rows if r["state"] == "pending"]
        if not pending:
            return "empty"
        pending.sort(key=lambda r: (-int(r["priority"] or 1), r["created"]))
        _execute(proj_dir, project_name, pending[0], rows, keep)
        return "ran"
    finally:
        release_lock(proj_dir)


def pick_project(only: str | None = None) -> str | None:
    """(-priority, last_activity_ts, nome) entre projetos enabled, sem lock
    vivo, com pending — round-robin ponderado starvation-free."""
    if not PROJECTS_ROOT.is_dir():
        return None
    candidates = []
    for proj_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not proj_dir.is_dir() or not (proj_dir / ".harness" / "config.toml").exists():
            continue
        name = proj_dir.name
        if only and name != only:
            continue
        cfg = read_config(proj_dir)
        if not cfg.get("enabled", True):
            continue
        if lock_is_live(proj_dir):
            continue
        rows = read_queue(proj_dir)
        if not any(r["state"] == "pending" for r in rows):
            continue
        priority = int(cfg.get("priority", 1))
        candidates.append((-priority, _last_activity(proj_dir), name))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


# ------------------------------------------------------------------------- cli


def cmd_add(a: argparse.Namespace) -> int:
    proj_dir = PROJECTS_ROOT / a.name
    if proj_dir.exists():
        print(f"projeto {a.name} já existe")
        return 1
    work_path = Path(a.path).expanduser().resolve()
    for sub in ("queue", "verify", "spec", "regression", "runs"):
        (proj_dir / sub).mkdir(parents=True, exist_ok=True)
    write_config(proj_dir, a.name, str(work_path), a.priority, enabled=True)
    (proj_dir / "queue.tsv").write_text("\t".join(QUEUE_HEADER) + "\n")
    (proj_dir / "spec" / "SPEC.md").write_text(f"---\nprojeto: {a.name}\n---\n\n# {a.name}\n")
    (proj_dir / "regression" / "MANIFEST.json").write_text(json.dumps({"checks": []}, indent=2) + "\n")
    (proj_dir / "MEMORY.md").write_text(f"# MEMORY — {a.name}\n")
    print(f"projeto {a.name} criado em {proj_dir} (work_path={work_path})")
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    if not PROJECTS_ROOT.is_dir():
        print("nenhum projeto")
        return 0
    found = False
    for proj_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not (proj_dir / ".harness" / "config.toml").exists():
            continue
        found = True
        cfg = read_config(proj_dir)
        rows = read_queue(proj_dir)
        pending = sum(1 for r in rows if r["state"] == "pending")
        lock = "locked" if lock_is_live(proj_dir) else "free"
        print(
            f"{proj_dir.name}\tpriority={cfg.get('priority', 1)}\t"
            f"enabled={cfg.get('enabled', True)}\tpending={pending}\tlock={lock}"
        )
    if not found:
        print("nenhum projeto")
    return 0


def cmd_queue_add(a: argparse.Namespace) -> int:
    proj_dir = PROJECTS_ROOT / a.name
    if not proj_dir.is_dir():
        print(f"projeto {a.name} não existe")
        return 1
    rows = read_queue(proj_dir)
    unit_id = f"{len(rows) + 1:04d}"
    (proj_dir / "queue" / f"{unit_id}.md").write_text(Path(a.prompt).read_text())
    (proj_dir / "verify" / f"{unit_id}.py").write_text(Path(a.verify).read_text())
    rows.append(
        {
            "id": unit_id,
            "state": "pending",
            "priority": str(a.priority),
            "created": _now(),
            "claimed_at": "",
            "prompt_file": f"queue/{unit_id}.md",
            "verify": f"verify/{unit_id}.py",
            "notes": a.title,
        }
    )
    write_queue(proj_dir, rows)
    graph.record_governance_event(a.name, "queue_add", "cli", detail=f"{unit_id}: {a.title}")
    print(f"unidade {unit_id} adicionada a {a.name}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    iterations = a.loop if a.loop else 1
    ran = 0
    for _ in range(iterations):
        target = a.project or pick_project()
        if target is None:
            print("[skip] nada pendente")
            break
        status = try_run_one(target, a.keep)
        if status == "ran":
            ran += 1
        elif status == "locked":
            print(f"[lock] {target} ocupado por outro processo — saindo")
            if a.project:
                break
        elif status == "missing":
            print(f"[erro] projeto {target} não existe")
            break
        else:  # empty
            print(f"[skip] {target} sem pendências")
            if a.project:
                break
    print(f"run: {ran} unidade(s) executada(s)")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    if not PROJECTS_ROOT.is_dir():
        print("nenhum projeto")
        return 0
    names = [a.name] if a.name else [
        p.name for p in sorted(PROJECTS_ROOT.iterdir())
        if (p / ".harness" / "config.toml").exists()
    ]
    if not names:
        print("nenhum projeto")
        return 0
    for name in names:
        proj_dir = PROJECTS_ROOT / name
        if not proj_dir.is_dir():
            print(f"{name}: não existe")
            continue
        rows = read_queue(proj_dir)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        lock = "locked" if lock_is_live(proj_dir) else "free"
        print(f"{name}: {counts or '{}'}  lock={lock}  last_activity={_last_activity(proj_dir) or '—'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("--path", required=True)
    p_add.add_argument("--priority", type=int, default=1)
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_queue = sub.add_parser("queue")
    p_queue.add_argument("name")
    q_sub = p_queue.add_subparsers(dest="qcmd", required=True)
    q_add = q_sub.add_parser("add")
    q_add.add_argument("title")
    q_add.add_argument("--prompt", required=True)
    q_add.add_argument("--verify", required=True)
    q_add.add_argument("--priority", type=int, default=1)
    q_add.set_defaults(func=cmd_queue_add)

    p_run = sub.add_parser("run")
    p_run.add_argument("--project", default=None)
    p_run.add_argument("--once", action="store_true")
    p_run.add_argument("--loop", type=int, default=0)
    p_run.add_argument("--keep", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status")
    p_status.add_argument("name", nargs="?", default=None)
    p_status.set_defaults(func=cmd_status)

    return ap


def main() -> int:
    ap = build_parser()
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
