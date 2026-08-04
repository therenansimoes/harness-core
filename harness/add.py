"""`harness add "<tarefa>"` — o harness autora a unit a partir de linguagem natural.

Mata a fricção de escrever `unit.toml` + `verify_cmd` à mão: UMA chamada barata
ao backend `claude_code` (haiku) recebe a tarefa + o contexto real do repo
registrado em `config/projects.toml` e devolve JSON com `{id_slug, prompt_md,
verify_cmd, kind}`.

Doutrina anti-Goodhart intacta: o `verify_cmd` é autorado ANTES do run e tem
que ser determinístico (build + grep/teste concreto). Comando vazio, trivial
(`true`) ou com passo manual é rejeitado aqui — nada escrito pela metade.

Unidade autorada por LLM nasce em `benchmarks/quarantine/` (mesmo formato do
`improve/synthesize`), nunca em `sealed`: promover é ato humano (`harness seal`).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import get_args

from harness.backends.registry import get_backend
from harness.improve.synthesize import QUARANTINE_DIR
from harness.types import ExecRequest, Kind

PROJECTS_FILE = Path("config/projects.toml")
UNIT_FILE = "unit.toml"
PROMPT_FILE = "prompt.md"
ADD_BACKEND = "claude_code"
ADD_MODEL = "haiku"
ADD_MAX_USD = 0.25
ADD_TIMEOUT_S = 180.0

KINDS = tuple(get_args(Kind))
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
README_NAMES = ("README.md", "README.rst", "README.txt", "README")
CONTEXT_HEAD_LINES = 60
PACKAGE_JSON_MAX = 2000
TREE_MAX_ENTRIES = 80
TREE_SKIP = (".git", "node_modules", ".venv", "__pycache__", ".cache", "dist")

# Não-determinismo óbvio: verify com passo humano não é régua, é opinião.
FORBIDDEN_VERIFY = (
    "review manually",
    "manually",
    "manualmente",
    "by hand",
    "a olho",
    "human",
    "humano",
    "visually",
    "visualmente",
    "julgue",
    "confira se",
)
# Verify que passa sempre não prova nada.
TRIVIAL_VERIFY = ("true", ":", "exit 0")

# `--ui`: a régua autorada não enxerga tela em branco. Este sufixo serve o dist,
# exige que exista CSS carregável e olha o screenshot (`harness ui-verify`).
UI_VERIFY_SUFFIX = " && harness ui-verify dist --expect-asset css"


class AddError(RuntimeError):
    """Falha de autoria: erro claro, nada escrito pela metade."""


def load_project_repo(name: str, projects_file: Path | None = None) -> Path:
    """Repo registrado em `config/projects.toml`. Ler, não inventar."""
    path = projects_file or PROJECTS_FILE
    if not path.is_file():
        raise AddError(f"registro de projetos não encontrado: {path.as_posix()}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entry = data.get("projects", {}).get(name)
    if not isinstance(entry, dict) or "repo" not in entry:
        known = ", ".join(sorted(data.get("projects", {}))) or "nenhum"
        raise AddError(f"projeto {name!r} sem repo em {path.as_posix()} (registrados: {known})")
    repo = Path(str(entry["repo"])).expanduser()
    if not repo.is_dir():
        raise AddError(f"repo do projeto {name!r} não é diretório: {repo}")
    return repo


def project_context(repo: Path) -> str:
    """Contexto REAL do repo: README + package.json + árvore parcial."""
    parts = [f"Raiz do repo: {repo}"]
    for rd in README_NAMES:
        p = repo / rd
        if p.is_file():
            head = "\n".join(
                p.read_text(encoding="utf-8", errors="replace").splitlines()[:CONTEXT_HEAD_LINES]
            )
            parts.append(f"--- {rd} (início) ---\n{head}")
            break
    pkg = repo / "package.json"
    if pkg.is_file():
        parts.append(
            "--- package.json ---\n"
            + pkg.read_text(encoding="utf-8", errors="replace")[:PACKAGE_JSON_MAX]
        )
    parts.append(f"--- árvore de arquivos (parcial) ---\n{_tree(repo)}")
    return "\n\n".join(parts)


def _tree(repo: Path) -> str:
    entries: list[str] = []
    stack = [repo]
    while stack and len(entries) < TREE_MAX_ENTRIES:
        cur = stack.pop(0)
        try:
            children = sorted(cur.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            if child.name in TREE_SKIP or child.name.startswith("."):
                continue
            rel = child.relative_to(repo).as_posix()
            entries.append(rel + "/" if child.is_dir() else rel)
            if child.is_dir():
                stack.append(child)
            if len(entries) >= TREE_MAX_ENTRIES:
                entries.append("…")
                break
    return "\n".join(entries) or "(repo vazio)"


def authoring_prompt(task: str, project: str, context: str) -> str:
    return f"""Você autora UMA unidade de trabalho para uma fila de agentes de código.

Tarefa (escrita por um humano): {task}
Projeto: {project}

Contexto do repositório (leia — cite apenas arquivos que existem aqui):
{context}

Responda APENAS com um objeto JSON, sem markdown e sem texto em volta, com as chaves:
- "id_slug": slug curto em kebab-case (a-z, 0-9, hífen) identificando a tarefa
- "prompt_md": instrução completa e autossuficiente em markdown para o agente \
executor, citando os arquivos reais do contexto
- "verify_cmd": comando shell DETERMINÍSTICO que prova que a tarefa ficou pronta \
(build e/ou greps/testes concretos no resultado esperado; sai 0 = pronto). \
PROIBIDO passo manual, revisão humana ou julgamento subjetivo
- "kind": um de {json.dumps(list(KINDS))}"""


def _call_author(prompt: str, model: str, max_usd: float) -> str:
    """UMA chamada ao backend claude_code. Seam de teste: mock troca esta função."""
    backend = get_backend(ADD_BACKEND)
    pre = backend.preflight()
    if not pre.ok:
        raise AddError(f"backend {ADD_BACKEND} indisponível: {pre.reason}")
    ws = Path(tempfile.mkdtemp(prefix="harness-add-"))
    trace = ws / "trace.jsonl"
    try:
        result = backend.execute(
            ExecRequest(
                prompt=prompt,
                workspace=ws / "empty",
                tools=("Read",),
                model=model,
                max_turns=1,
                timeout_s=ADD_TIMEOUT_S,
                trace_path=trace,
            )
        )
        if not result.ok:
            raise AddError(
                f"chamada de autoria falhou: exit_reason={result.exit_reason} (trace descartado)"
            )
        if result.cost_usd is not None and result.cost_usd > max_usd:
            raise AddError(
                f"autoria custou ${result.cost_usd:.4f} > teto ${max_usd:.2f} — unit não gravada"
            )
        text = _result_text(trace)
        if not text:
            raise AddError("backend não devolveu texto de resposta")
        return text
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _result_text(trace: Path) -> str:
    """O `--output-format json` do CLI põe a resposta em `result`."""
    try:
        lines = trace.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            return obj["result"]
    return ""


def parse_author_json(text: str) -> dict[str, str]:
    """Extrai e valida o JSON da resposta. Qualquer desvio é AddError."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AddError(f"resposta sem objeto JSON: {text[:200]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AddError(f"JSON inválido na resposta: {exc}") from None
    if not isinstance(data, dict):
        raise AddError("resposta JSON não é objeto")
    missing = [k for k in ("id_slug", "prompt_md", "verify_cmd", "kind") if k not in data]
    if missing:
        raise AddError(f"resposta sem campos: {', '.join(missing)}")
    slug = str(data["id_slug"]).strip()
    if not SLUG_RE.fullmatch(slug):
        raise AddError(f"id_slug inválido: {slug!r} (esperado kebab-case a-z0-9)")
    prompt_md = str(data["prompt_md"]).strip()
    if not prompt_md:
        raise AddError("prompt_md vazio")
    verify_cmd = str(data["verify_cmd"]).strip()
    validate_verify_cmd(verify_cmd)
    kind = str(data["kind"]).strip()
    if kind not in KINDS:
        raise AddError(f"kind inválido: {kind!r} (esperado um de {', '.join(KINDS)})")
    return {"id_slug": slug, "prompt_md": prompt_md, "verify_cmd": verify_cmd, "kind": kind}


def validate_verify_cmd(cmd: str) -> None:
    """Régua autorada antes do run: determinística ou nada."""
    if not cmd:
        raise AddError("verify_cmd vazio — unit sem régua não entra na fila")
    if cmd.lower() in TRIVIAL_VERIFY:
        raise AddError(f"verify_cmd trivial ({cmd!r}) não prova nada")
    low = cmd.lower()
    hits = [w for w in FORBIDDEN_VERIFY if w in low]
    if hits:
        raise AddError(
            f"verify_cmd não-determinístico ({', '.join(hits)!s}): {cmd!r} — "
            f"a régua tem que ser build + greps/testes concretos"
        )


def _s(value: str) -> str:
    # json.dumps produz uma basic string válida em TOML (escapes compatíveis)
    return json.dumps(value, ensure_ascii=False)


def render_unit_toml(spec: dict[str, str], project: str, task: str) -> str:
    """Formato do `improve/synthesize`: campos da unit + `[origin]` da procedência."""
    return "\n".join(
        [
            "# Autorado por `harness add` — verify_cmd escrito ANTES do run.",
            "# Quarentena: promover para benchmarks/sealed/ é ato humano.",
            f"id = {_s(spec['id_slug'])}",
            f"kind = {_s(spec['kind'])}",
            f"prompt = {_s(spec['prompt_md'])}",
            f"verify_cmd = {_s(spec['verify_cmd'])}",
            "",
            "[origin]",
            'authored_by = "harness add"',
            f"project = {_s(project)}",
            f"task = {_s(task)}",
            "",
        ]
    )


def add(
    task: str,
    project: str,
    *,
    model: str = ADD_MODEL,
    max_usd: float = ADD_MAX_USD,
    dry: bool = False,
    ui: bool = False,
    projects_file: Path | None = None,
    out_dir: Path | None = None,
    out=None,
) -> Path | None:
    """Autora a unit e grava em `benchmarks/quarantine/<slug>/`. `dry` só mostra."""
    out = out if out is not None else sys.stdout
    task = task.strip()
    if not task:
        raise AddError("tarefa vazia")
    repo = load_project_repo(project, projects_file)
    context = project_context(repo)
    text = _call_author(authoring_prompt(task, project, context), model, max_usd)
    spec = parse_author_json(text)
    # Sufixo DEPOIS da validação: o verify autorado continua tendo de se sustentar
    # sozinho; o ui-verify é exigência extra, não muleta para régua fraca.
    if ui and "harness ui-verify" not in spec["verify_cmd"]:
        spec["verify_cmd"] += UI_VERIFY_SUFFIX
    content = render_unit_toml(spec, project, task)
    tomllib.loads(content)  # round-trip: o que sai daqui SEMPRE carrega

    unit_dir = (out_dir or QUARANTINE_DIR) / spec["id_slug"]
    if dry:
        print(f"--dry: nada gravado. Destino seria {unit_dir.as_posix()}", file=out)
        print(f"--- {UNIT_FILE} ---\n{content}", file=out)
        print(f"--- {PROMPT_FILE} ---\n{spec['prompt_md']}", file=out)
        return None

    if unit_dir.exists():
        raise AddError(f"unit já existe em quarentena: {unit_dir.as_posix()}")
    unit_dir.mkdir(parents=True)
    try:
        # unit.toml por ÚLTIMO: quem varre a quarentena só enxerga dir com ele,
        # então uma falha no meio nunca deixa unidade meio escrita elegível.
        (unit_dir / PROMPT_FILE).write_text(spec["prompt_md"] + "\n", encoding="utf-8")
        (unit_dir / UNIT_FILE).write_text(content, encoding="utf-8")
    except OSError:
        shutil.rmtree(unit_dir, ignore_errors=True)
        raise
    from harness.cli import load_unit  # tardio: cli importa este módulo

    load_unit(unit_dir)  # autochecagem: o que gravamos carrega de verdade
    print(
        f"add {spec['id_slug']} kind={spec['kind']} -> {unit_dir.as_posix()} "
        f"(rode: harness run --unit {unit_dir.as_posix()})",
        file=out,
    )
    return unit_dir
