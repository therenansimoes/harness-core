"""Registro de projetos reais: repos git onde o harness entrega branches.

`harness init <repo> --name <nome>` grava a entrada em `config/projects.toml`.
Uma unidade com `project = "<nome>"` roda num git worktree do repo real
(branch efêmera `harness/<run_id>` a partir do HEAD); no accept a branch vira
`harness/<unit_id>` com o commit da entrega e fica para review humano — a
working tree principal do repo nunca é tocada, e merge é sempre do humano.

O worktree é opt-in: sem `project` na unidade nada aqui é chamado e o
provisionamento default (cópia em `$HARNESS_DATA_DIR/ws/<run_id>`) segue igual.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from harness.routing import config_dir

PROJECTS_FILE = "projects.toml"
# Mesma tabela que o `harness add` lê (`[projects.<nome>]`): registro único, um
# init alimenta os dois caminhos.
TABLE = "projects"
QUEUE_DONE = "done"
QUEUE_STUCK = "stuck"
UNIT_FILE = "unit.toml"


@dataclass(frozen=True)
class Project:
    name: str
    repo: Path
    build_cmd: str | None = None
    verify_default: str | None = None
    queue_dir: Path | None = None


def projects_path() -> Path:
    return config_dir() / PROJECTS_FILE


def load_projects(path: Path | None = None) -> dict[str, Project]:
    p = path or projects_path()
    if not p.is_file():
        return {}
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    out: dict[str, Project] = {}
    for name, raw in data.get(TABLE, {}).items():
        if not isinstance(raw, dict) or "repo" not in raw:
            continue
        out[name] = Project(
            name=name,
            repo=Path(raw["repo"]),
            build_cmd=raw.get("build_cmd"),
            verify_default=raw.get("verify_default"),
            queue_dir=Path(raw["queue_dir"]) if raw.get("queue_dir") else None,
        )
    return out


def get_project(name: str, path: Path | None = None) -> Project:
    projs = load_projects(path)
    if name not in projs:
        raise ValueError(
            f"projeto {name!r} não registrado em {path or projects_path()} — "
            "rode `harness init <repo> --name " + name + "`"
        )
    return projs[name]


def init_project(
    repo: Path | str,
    name: str,
    build_cmd: str | None = None,
    verify_default: str | None = None,
    queue_dir: Path | str | None = None,
    path: Path | None = None,
) -> Project:
    """Registra (ou atualiza) um projeto. Idempotente: re-init sobrescreve a
    entrada com os valores dados; os demais projetos ficam intactos."""
    repo = Path(repo).expanduser().resolve()
    proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise ValueError(f"{repo} não é repositório git")
    qdir = Path(queue_dir) if queue_dir else Path("projects") / name / "queue"
    proj = Project(
        name=name,
        repo=repo,
        build_cmd=build_cmd,
        verify_default=verify_default,
        # Absoluto no registro: `harness status` roda de qualquer cwd.
        queue_dir=qdir.expanduser().resolve(),
    )
    target = path or projects_path()
    projs = load_projects(target)
    projs[name] = proj
    _write(projs, target)
    proj.queue_dir.mkdir(parents=True, exist_ok=True)
    return proj


def queue_counts(proj: Project) -> tuple[int, int, int]:
    """`(fila, done, stuck)` da fila do projeto. Fila = dir com `unit.toml`."""
    q = proj.queue_dir
    if not q or not q.is_dir():
        return (0, 0, 0)
    fila = sum(
        1
        for p in q.iterdir()
        if p.is_dir()
        and p.name not in (QUEUE_DONE, QUEUE_STUCK)
        and (p / UNIT_FILE).is_file()
    )
    return (fila, _bucket(q / QUEUE_DONE), _bucket(q / QUEUE_STUCK))


# --- entrega em branch --------------------------------------------------------


def run_branch(run_id: str) -> str:
    """Branch efêmera do run dentro do repo do projeto."""
    return f"harness/{run_id}"


def delivery_branch(unit_id: str) -> str:
    """Branch da entrega, o que fica para review humano."""
    safe = re.sub(r"[^A-Za-z0-9._/-]+", "-", unit_id).strip("-/.") or "unit"
    return f"harness/{safe}"


def deliver(
    ws: Path,
    unit_id: str,
    run_id: str,
    cost_usd: float | None = None,
    exclude: tuple[str, ...] = (),
) -> tuple[str, str | None]:
    """Commita o que o run escreveu no worktree e renomeia a branch efêmera para
    `harness/<unit_id>`. Devolve `(branch, commit)`; commit é None quando o run
    não mudou nada. Nada de merge — a branch fica para review humano."""
    excludes = [f":(exclude){name}" for name in exclude]
    _git(ws, "add", "-A", "--", ".", *excludes)
    commit = None
    if _git(ws, "diff", "--cached", "--quiet").returncode != 0:
        msg = (
            f"harness: {unit_id}\n\nrun_id={run_id}\n"
            f"cost_usd={f'{cost_usd:.4f}' if cost_usd is not None else 'desconhecido'}"
        )
        proc = _git(ws, "-c", "user.name=harness",
                    "-c", "user.email=harness@harness.local", "commit", "-m", msg)
        if proc.returncode != 0:
            raise RuntimeError(f"accept: git commit falhou — {proc.stderr.strip()}")
        commit = _git(ws, "rev-parse", "HEAD").stdout.strip()

    branch = delivery_branch(unit_id)
    # `-M` força: re-entrega da mesma unidade substitui a branch antiga. Se o
    # rename falhar (branch presa noutro worktree), a entrega fica na branch
    # do run — pior nome, mesma evidência.
    if _git(ws, "branch", "-M", branch).returncode != 0:
        branch = run_branch(run_id)
    return (branch, commit)


def discard_run_branch(repo: Path, run_id: str) -> None:
    """Mata a branch efêmera do run. Depois de revert/escalate o repo volta a
    não ter vestígio nenhum da tentativa."""
    _git(repo, "branch", "-D", run_branch(run_id))


def _bucket(root: Path) -> int:
    return sum(1 for p in root.iterdir() if p.is_dir()) if root.is_dir() else 0


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )


def _write(projs: dict[str, Project], path: Path) -> None:
    # json.dumps produz string TOML básica válida (aspas + escapes).
    lines: list[str] = []
    for name in sorted(projs):
        pr = projs[name]
        lines.append(f"[{TABLE}.{json.dumps(name)}]")
        lines.append(f"repo = {json.dumps(str(pr.repo))}")
        if pr.build_cmd:
            lines.append(f"build_cmd = {json.dumps(pr.build_cmd)}")
        if pr.verify_default:
            lines.append(f"verify_default = {json.dumps(pr.verify_default)}")
        if pr.queue_dir:
            lines.append(f"queue_dir = {json.dumps(str(pr.queue_dir))}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
