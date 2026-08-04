"""Registro de projetos reais: repos git onde o harness entrega branches.

`harness init <repo> --name <nome>` grava a entrada em `config/projects.toml`.
Uma unidade com `project = "<nome>"` roda num git worktree do repo real
(branch efêmera `harness/<run_id>` a partir do HEAD); no accept a branch vira
`harness/<unit_id>` com o commit da entrega e fica para review humano.

Na fila progressiva o accept é seguido de `integrate`, que faz merge `--no-ff`
dessa branch no branch default do repo-alvo: sem isso a unidade seguinte
provisiona o worktree do HEAD e não vê nada do que a anterior entregou. Conflito
nunca é resolvido aqui — o merge é abortado e quem chama para.

O worktree é opt-in: sem `project` na unidade nada aqui é chamado e o
provisionamento default (cópia em `$HARNESS_DATA_DIR/ws/<run_id>`) segue igual.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
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
MILESTONES_FILE = "MILESTONES.toml"
MILESTONE_TABLE = "milestone"


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


# --- marcos (agrupamento declarado de unidades) --------------------------------


def milestones_path(proj: Project) -> Path:
    """`projects/<nome>/MILESTONES.toml`: irmão da fila, para o arquivo andar
    junto das unidades que ele agrupa."""
    base = proj.queue_dir.parent if proj.queue_dir else Path("projects") / proj.name
    return base / MILESTONES_FILE


def milestones(proj: Project) -> list[dict]:
    """Marcos declarados: `[[milestone]] name = "..." units = ["u1", "u2"]`.

    Marco é opcional e puro dado — nada aqui pode derrubar `harness status`.
    Ausente, ilegível ou sem `[[milestone]]` => `[]`; entrada sem `name` ou sem
    `units` usável é ignorada com aviso, porque marco que some calado é pior
    que marco recusado.
    """
    path = milestones_path(proj)
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"milestones: {path} inválido, ignorando — {exc}", file=sys.stderr)
        return []

    raw = data.get(MILESTONE_TABLE)
    if not isinstance(raw, list):
        if raw is not None:
            print(
                f"milestones: {path} sem [[{MILESTONE_TABLE}]], ignorando",
                file=sys.stderr,
            )
        return []

    out: list[dict] = []
    for i, entry in enumerate(raw):
        name = str(entry.get("name", "")).strip() if isinstance(entry, dict) else ""
        units = entry.get("units") if isinstance(entry, dict) else None
        if not name:
            print(f"milestones: [[{MILESTONE_TABLE}]] #{i + 1} sem 'name', ignorado "
                  f"({path})", file=sys.stderr)
            continue
        if not isinstance(units, list):
            print(f"milestones: {name!r} sem lista 'units', ignorado ({path})",
                  file=sys.stderr)
            continue
        ids = [str(u).strip() for u in units if str(u).strip()]
        if not ids:
            print(f"milestones: {name!r} com 'units' vazio, ignorado ({path})",
                  file=sys.stderr)
            continue
        out.append({"name": name, "units": ids})
    return out


def milestone_progress(proj: Project) -> list[tuple[str, int, int]]:
    """`(nome, feitas, total)` por marco. Feita = unidade em `queue/done/`
    (match por basename do diretório) — a unidade parada em `stuck/` ou ainda
    na fila conta como não feita, que é o ponto do marco."""
    marcos = milestones(proj)
    if not marcos:
        return []
    done = set()
    if proj.queue_dir:
        d = proj.queue_dir / QUEUE_DONE
        if d.is_dir():
            done = {p.name for p in d.iterdir() if p.is_dir()}
    return [
        (m["name"], sum(1 for u in m["units"] if u in done), len(m["units"]))
        for m in marcos
    ]


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
    # `.harness` sempre fora: é o scratch do run (log da régua, backups do
    # edit_range, cache do web_fetch) e nada disso pertence à branch de entrega.
    excludes = [f":(exclude){name}" for name in (*exclude, ".harness")]
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


class IntegrateError(RuntimeError):
    """Integração falhou. Erro distinguível de propósito: quem chama (a fila)
    para aqui e nunca tenta resolver — conflito é assunto de humano.

    `conflict=True` quando o merge abriu conflito (e já foi abortado); False para
    pré-condição não satisfeita (working tree suja, repo inválido, branch presa).
    """

    def __init__(self, msg: str, *, conflict: bool = False) -> None:
        super().__init__(msg)
        self.conflict = conflict


def default_branch(repo: Path) -> str:
    """Branch de integração do repo-alvo: `origin/HEAD` quando existe, senão a
    branch atual, senão `master`. Repo local sem remote é o caso comum nos
    testes e em repo de bancada — por isso o fallback, não o erro."""
    proc = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    if name:
        return name.split("/", 1)[1] if name.startswith("origin/") else name
    proc = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name if name and name != "HEAD" else "master"


def integrate(project: Project | str, unit_id: str, path: Path | None = None) -> str:
    """Faz `harness/<unit_id>` entrar no branch default do repo-alvo.

    Sem isto a fila progressiva é semanticamente quebrada: cada unidade provisiona
    o worktree a partir do HEAD do repo, então a unidade N não vê nada do que as
    anteriores entregaram nas suas branches. O merge é `--no-ff` (a entrega fica
    visível como unidade na história) e a branch de entrega **não** é apagada:
    review humano continua possível depois da composição.

    Exige working tree limpa nos arquivos versionados; arquivo não versionado é
    ignorado (build artifact é comum no repo-alvo e não bloqueia merge — se
    bloquear, o git recusa com mensagem própria e cai no mesmo erro daqui).

    Devolve a linha de log do que aconteceu. Levanta `IntegrateError` em qualquer
    falha, com `conflict=True` quando houve conflito — nesse caso o merge já foi
    abortado e o repo volta ao estado anterior.
    """
    proj = project if isinstance(project, Project) else get_project(project, path)
    repo = proj.repo
    proc = _git(repo, "rev-parse", "--is-inside-work-tree")
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise IntegrateError(f"integrate: {repo} não é repositório git")

    branch = delivery_branch(unit_id)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode:
        # Accept sem branch de entrega: unidade que não rodou em worktree do
        # projeto (ou run que não mudou arquivo nenhum). Nada a compor, e travar
        # a fila por isso seria pior do que seguir.
        return f"integrate: {branch} não existe — nada a integrar"

    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise IntegrateError(
            f"integrate: working tree de {repo} suja — commite ou limpe antes:\n"
            + dirty
        )

    target = default_branch(repo)
    proc = _git(repo, "checkout", target)
    if proc.returncode != 0:
        raise IntegrateError(
            f"integrate: checkout de {target} em {repo} falhou — {proc.stderr.strip()}"
        )

    proc = _git(
        repo, "-c", "user.name=harness", "-c", "user.email=harness@harness.local",
        "merge", "--no-ff", "-m", f"harness: integrate {unit_id}", branch,
    )
    if proc.returncode != 0:
        detalhe = (proc.stdout + proc.stderr).strip()
        _git(repo, "merge", "--abort")
        raise IntegrateError(
            f"integrate: merge de {branch} em {target} falhou e foi abortado — "
            f"resolva na mão e reponha a unidade na fila:\n{detalhe}",
            conflict=True,
        )
    head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    return f"integrate: {branch} -> {target} ({head})"


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
