"""`harness serve`: endpoint OpenAI-compatível local para Cursor conversar
com este repo.

stdlib apenas — `uv.lock` é immutable no genoma (`config/genome.toml`,
AGENTS.md "Genome zones"), então este módulo não pode puxar FastAPI/uvicorn
nem qualquer dependência nova: um builder não tem como legalmente tocar o
lock. O grão fica em dois lugares que já resolveram o mesmo problema —
`scripts/fleet_gateway.py` (rotas `/v1/models`/`/v1/chat/completions`, SSE,
`ThreadingHTTPServer`) e `harness/triggers/webhook.py::serve_webhook`
(`port=0` + `on_bind` + `max_requests` para o teste subir servidor de
verdade sem depender de porta fixa). A gramática do chunk SSE é escrita à
mão aqui em vez de vir de um framework.

Loopback por default (`127.0.0.1`): expor na rede é decisão explícita de
quem sobe o processo, com aviso no stderr — não comportamento de fábrica.

Autenticação segue o mesmo padrão de `harness/triggers/webhook.py`: com
`api_key` configurada (via `--api-key` ou `HARNESS_SERVE_KEY`), toda request
precisa do header `Authorization: Bearer <api_key>` (comparação em tempo
constante) ou leva 401. Fora do loopback e sem key, o servidor SOBE
recusando tudo com 403 em vez de subir aberto — mesma regra fail-closed do
webhook, um endpoint de rede sem segredo não é "conveniente", é porta
destrancada.

A segurança do módulo é estrutural, não é uma checagem: `_COMMANDS` é um
dict FECHADO de comandos de leitura e de baixo risco (status, ready, queue,
history, market, new, close, do, where). Não existe caminho, daqui, para
`market approve`, `selfapprove approve/undo`, `seal` ou qualquer escrita em
`config/genome.toml` — quem quer aprovar algo continua tendo de usar a CLI
na mão. `/do` dispara em segundo plano com `--no-apply`: uma mensagem de
chat não merge sozinha na branch default.

Workspace do Cursor: o corpo da request injeta um bloco `<user_info>` com o
path do projeto aberto no editor (`Workspace Path: ...`, às vezes só `Is
directory a git repo: Yes, at ...`). `extract_workspace`/`validate_workspace`
puxam esse path de forma determinística — sem MCP, sem "model por projeto"
(o modelo continua sendo só `harness`) — e só o aceitam contra uma allowlist
(`~/projects` + repos registrados via `harness init`). Qualquer coisa que não
bata a allowlist degrada para o comportamento de hoje (canal de controle só
do harness-core); `/where` mostra exatamente o que foi lido e o veredito,
porque o formato do prompt do Cursor pode driftar em qualquer update deles.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from harness import paths, trust_boundary
from harness.governor.governor import load_gov
from harness.ledger import store

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
API_KEY_ENV = "HARNESS_SERVE_KEY"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
UNAUTHORIZED = 401
FORBIDDEN = 403  # fail-closed: fora do loopback e sem key, mesmo 403 do webhook
MODEL_ID = "harness"
EXECUTOR_PREFIX = "harness:"
STRICT_MODEL_ENV = "HARNESS_SERVE_STRICT_MODEL"  # "0" => id desconhecido vira auto
MODEL_NOT_FOUND = 404
MAX_MODEL_ID_CHARS = 120
MAX_USD_CAP = 5.0
MAX_RUNNING_JOBS = 1
CHUNK_CHARS = 120  # tamanho da fatia de cada frame SSE
STATE_MAX_CHARS = 2000  # mesmo raciocínio de deepagents_backend.TARGET_CONSTITUTION_MAX_CHARS
BASE_URL_ENV = "OPENAI_BASE_URL"  # mesma var de vision.py / deepagents_backend
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
KEY_ENV = "OPENAI_API_KEY"
OPENAI_PREFIX = "openai:"
LLM_TIMEOUT_S = 120.0
PROBE_TIMEOUT_S = 3.0
BD_TIMEOUT_S = 15.0

JOBS_SUBDIR = ("serve", "jobs")

# --------------------------------------------------------------------------- workspace detection: constantes

USER_INFO_RE = re.compile(r"<user_info>(.*?)</user_info>", re.DOTALL | re.IGNORECASE)
WORKSPACE_RE = re.compile(r"^\s*Workspace Paths?:\s*(?P<path>[^\n]+?)\s*$", re.MULTILINE | re.IGNORECASE)
GIT_REPO_RE = re.compile(
    r"^\s*Is directory a git repo:\s*Yes,?\s*at\s+(?P<path>[^\n]+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL | re.IGNORECASE)
SCAN_CHARS_PER_MSG = 20_000  # o bloco user_info é pequeno; teto por mensagem, não por índice
MAX_BODY_BYTES = 8 * 1024 * 1024  # 100+ mensagens com tool results é entrada de cliente sem teto
PAYLOAD_TOO_LARGE = 413
MAX_PATH_CHARS = 4096
WORKSPACE_ROOTS_ENV = "HARNESS_SERVE_WORKSPACE_ROOTS"
GIT_TIMEOUT_S = 5.0
README_NAMES = ("README.md", "README.rst", "README.txt", "AGENTS.md")
README_MAX_BYTES, README_MAX_LINES, README_MAX_CHARS = 8192, 30, 1200
TREE_MAX_ENTRIES = 40


def default_workspace_roots() -> tuple[Path, ...]:
    return (Path.home() / "projects",)  # call-time: o teste monkeypatcha HOME


@dataclass(frozen=True)
class Detection:
    """Resultado de uma tentativa de extrair/validar o workspace do payload
    do Cursor. `raw` é exatamente o que veio (para o `/where` mostrar o que
    foi lido, sem reinterpretar); `path`/`project` só existem quando
    `verdict == "ok"`."""

    raw: str | None
    path: Path | None
    verdict: str  # "ok" | "ausente" | "inválido" | "relativo" | "não é diretório" | "fora dos roots"
    project: str | None


@dataclass(frozen=True)
class ServeContext:
    """Estado pinado no boot do servidor. `cwd` nunca é `Path.cwd()` do
    request — o repo que o `/do` usa é o que estava aberto quando o processo
    subiu, não o que estiver no ambiente da thread que atende a conexão.

    Por request, `ctx_for_request` pode devolver uma cópia com `cwd` trocado
    para o workspace detectado no payload do Cursor: `home` guarda o cwd de
    boot original nesse caso, `project` o nome registrado (se houver) e
    `detection` o veredito bruto — aceito ou não, sempre presente depois de
    `ctx_for_request` rodar.

    `executor`/`requested_model` são resolvidos por request pelo `do_POST`
    (campo `model` do corpo OpenAI-like): `executor` é o registro já
    resolvido (`None` = comportamento de hoje, auto), `requested_model` é a
    string CRUA que o cliente mandou — só existe pra `/where` mostrar o que
    foi pedido de verdade, mesmo quando a resolução degrada pra auto."""

    cwd: Path
    now: Callable[[], float] = field(default=time.time)
    home: Path | None = None
    project: str | None = None
    detection: Detection | None = None
    workspace_roots: tuple[Path, ...] = ()
    executor: Executor | None = None
    requested_model: str | None = None


# --------------------------------------------------------------------------- executores: registro por trás do campo "model"


@dataclass(frozen=True)
class Executor:
    """Um id oferecido no campo `model` do protocolo OpenAI. `auto` é o único
    que fala pelo router de verdade — os demais são um PIN: `backend`/
    `run_model` vêm só do registro (`executors()`), nunca do payload do
    cliente, e `local` decide se este turno de CHAT pode responder (chat é
    estruturalmente local; executor pago só roda via `/do`)."""

    id: str  # "harness", "harness:local", ...
    tier: str  # "" no auto
    backend: str  # "" no auto (nunca vem do payload)
    run_model: str  # modelo pro argv do `harness do`; "" = o backend escolhe
    local: bool  # roda no runtime desta máquina => responde chat, custo $0
    label: str  # 1 linha descritiva


def auto_executor() -> Executor:
    return Executor(MODEL_ID, "", "", "", True, "auto do harness — o router escolhe o executor no /do")


def _runs_local(backend: str, model: str) -> bool:
    """Mesma regra de `harness.routing.adapters.runs_local` (import lazy: o
    caminho de chat não depende de routing pra responder). Fallback embutido
    é a regra real (adapters.py:243-245), não uma aproximação."""
    try:
        from harness.routing.adapters import runs_local

        return runs_local(backend, model)
    except Exception:
        return backend == "deepagents" and model.startswith(OPENAI_PREFIX)


def _tier_alias(t: Any, taken: set[str]) -> str:
    """Nome curto e determinístico pro executor deste tier: "local" quando o
    tier roda no runtime da máquina, senão o miolo do nome de modelo (sem o
    prefixo do provedor) ou, na falta de modelo (tier "o CLI escolhe"), o
    backend. Colisão (dois tiers com o mesmo miolo) desempata com o nome do
    tier — chamado em ordem de `cost_rank`, então o resultado é estável."""
    if _runs_local(t.backend, t.model):
        alias = "local"
    else:
        alias = t.model.rsplit("/", 1)[-1].removeprefix(OPENAI_PREFIX).strip() or t.backend
    if alias in taken:
        alias = f"{alias}-{t.name}"
    return alias


def executors() -> list[Executor]:
    """`auto` primeiro, depois um `Executor` por `[[tier]]` de
    `config/models.toml`, na ordem de `cost_rank`. NUNCA levanta: um
    `models.toml` ilegível não pode derrubar `/v1/models` (o Cursor faz
    polling nele) — degrada pro auto sozinho, com uma linha no stderr. SEM
    cache: config é zona mutável do genoma, uma leitura por chamada."""
    try:
        from harness.routing.router import load_config, tiers

        cfg = load_config()
        taken: set[str] = set()
        execs = [auto_executor()]
        for t in tiers(cfg):
            alias = _tier_alias(t, taken)
            taken.add(alias)
            execs.append(
                Executor(
                    id=f"{EXECUTOR_PREFIX}{alias}",
                    tier=t.name,
                    backend=t.backend,
                    run_model=t.model,
                    local=_runs_local(t.backend, t.model),
                    label=f"{t.name} · {t.backend}" + (f" · {t.model}" if t.model else ""),
                )
            )
        return execs
    except Exception as exc:
        print(f"[serve] models.toml ilegível — só o executor auto: {exc}", file=sys.stderr)
        return [auto_executor()]


def resolve_executor(raw: str | None) -> Executor | None:
    """Única porta de entrada do campo `model` pro resto do módulo: devolve
    um `Executor` do REGISTRO (nunca um construído com a string do cliente) ou
    `None` (id desconhecido — quem chama decide 404 ou degradar pro auto).
    None/vazio/só espaço => auto (comportamento de hoje, sem campo `model`).
    Comparação case-insensitive contra o id primário e contra o alias oculto
    `harness:<tier>` — mas só quando o tier existe (o auto tem `tier == ""` e
    não pode casar contra `harness:` vazio)."""
    if raw is None or not raw.strip():
        return auto_executor()
    key = raw.strip()
    if len(key) > MAX_MODEL_ID_CHARS:
        return None
    key_lower = key.lower()
    for e in executors():
        if key_lower == e.id.lower():
            return e
        if e.tier and key_lower == f"{EXECUTOR_PREFIX}{e.tier}".lower():
            return e
    return None


# --------------------------------------------------------------------------- workspace detection: leitura + validação


def message_text(msg: dict) -> str:
    """Lugar único do shape `content` str-ou-partes do payload OpenAI-like:
    string vai direto, lista concatena o texto das partes `{"type": "text"}`,
    qualquer outra coisa vira "" (mensagem sem texto, ex. só tool_calls)."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def user_query_text(raw: str) -> str:
    """O Cursor embrulha o texto digitado em `<user_query>...</user_query>`
    (às vezes precedido de `<timestamp>`); sem desembrulhar, `/status` chega
    aqui como `"<timestamp>...</timestamp>\\n<user_query>\\n/status\\n</user_query>"`
    e o `startswith("/")` do router nunca bate — bug real, visível pro
    usuário (todo slash command digitado no Cursor ia parar no LM). Último
    bloco ganha quando há mais de um; sem tag, passthrough intacto (corpo
    puro de teste ou cliente não-Cursor)."""
    matches = USER_QUERY_RE.findall(raw)
    return matches[-1].strip() if matches else raw


def extract_workspace(messages: list[dict]) -> str | None:
    """Varre TODAS as mensagens em ordem (sem teto de índice — o bloco pode
    se mover em update do Cursor) atrás de um `<user_info>` com o path do
    workspace. Primeiro match ganha: só o que está DENTRO do wrapper conta
    (um "Workspace Path:" solto ou ecoado depois na conversa não é sinal), e
    a decisão para no primeiro bloco que resolve — isso tira da mesa um path
    colado/ecoado mais tarde por outra mensagem."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = message_text(msg)[:SCAN_CHARS_PER_MSG]
        if "<user_info" not in text.lower():
            continue
        for block in USER_INFO_RE.findall(text):
            m = WORKSPACE_RE.search(block) or GIT_REPO_RE.search(block)
            if m:
                return m.group("path")
    return None


def _safe_for_log(raw: str) -> str:
    """Sem control chars, clipado — o que vai pro stderr não pode carregar
    escape sequence nem virar log gigante."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "?", raw)
    return cleaned[:200]


def _project_name_for(repo: Path) -> str | None:
    """Nome registrado em `harness init` cujo repo resolve pro mesmo path, ou
    None (não registrado, ou registro ilegível — fail-open, isto só
    enriquece a resposta, nunca decide se o path é aceito)."""
    try:
        from harness import projects as harness_projects

        for name, proj in harness_projects.load_projects().items():
            try:
                if proj.repo.resolve() == repo:
                    return name
            except OSError:
                continue
    except Exception:
        return None
    return None


def allowed_roots(ctx: ServeContext) -> tuple[Path, ...]:
    """Roots do boot (`ctx.workspace_roots`) mais os repos que o usuário já
    registrou em `harness init` — um repo registrado explicitamente é
    confiável mesmo fora de `~/projects`. Registro ausente/ilegível: skip,
    não derruba a detecção."""
    roots = list(ctx.workspace_roots)
    try:
        from harness import projects as harness_projects

        for proj in harness_projects.load_projects().values():
            try:
                roots.append(proj.repo.resolve())
            except OSError:
                continue
    except Exception:
        pass
    return tuple(roots)


def validate_workspace(raw: str | None, roots: tuple[Path, ...]) -> Detection:
    """Allowlist, nunca inspeção de string: todo veredito != "ok" devolve
    `path=None` e a chamada degrada pro comportamento sem workspace. Falha
    "acionável" (alguém mandou um path e ele não bateu) vira 1 linha no
    stderr; "ausente" (nenhum `<user_info>` no payload) é o caso normal de
    qualquer cliente que não é o Cursor e fica calado — `/where` já cobre a
    visibilidade desse caso sob pedido."""
    if raw is None:
        return Detection(raw, None, "ausente", None)
    if len(raw.strip()) > MAX_PATH_CHARS or "\x00" in raw:
        print(f"[serve] workspace ignorado (inválido): {_safe_for_log(raw)}", file=sys.stderr)
        return Detection(raw, None, "inválido", None)
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        print(f"[serve] workspace ignorado (relativo): {_safe_for_log(raw)}", file=sys.stderr)
        return Detection(raw, None, "relativo", None)
    # resolve() ANTES do teste de root: colapsa ".." e segue symlink — é isto
    # que fecha traversal (`{root}/../../etc` ou um symlink pra `/etc` dentro
    # do root nunca sobrevive ao teste de allowlist abaixo).
    p = p.resolve()
    if not p.is_dir():
        print(f"[serve] workspace ignorado (não é diretório): {_safe_for_log(raw)}", file=sys.stderr)
        return Detection(raw, None, "não é diretório", None)
    if not any(p == r or p.is_relative_to(r) for r in roots):
        print(f"[serve] workspace ignorado (fora dos roots): {_safe_for_log(raw)}", file=sys.stderr)
        return Detection(raw, None, "fora dos roots", None)
    return Detection(raw, p, "ok", _project_name_for(p))


def ctx_for_request(messages: list[dict], ctx: ServeContext) -> ServeContext:
    """Detecção por request: `ctx.cwd` de boot nunca muda por conta própria —
    só uma detecção aceita ("ok") troca `cwd` pro workspace, guardando o cwd
    de boot em `home`. Qualquer outro veredito só anexa `detection` (pro
    `/where` e pro roteamento saberem o que aconteceu) e mantém `cwd` como
    estava."""
    det = validate_workspace(extract_workspace(messages), allowed_roots(ctx))
    if det.verdict == "ok" and det.path is not None:
        return replace(ctx, cwd=det.path, home=ctx.cwd, project=det.project, detection=det)
    return replace(ctx, detection=det)


# --------------------------------------------------------------------------- seams


def _bd(*args: str, cwd: Path | None = None, timeout_s: float = BD_TIMEOUT_S) -> tuple[int, str]:
    """`bd <args>` → (rc, stdout+stderr). Indireção para o teste trocar por
    um fake, mesma convenção de `_http_post`/`_http_get` abaixo. `cwd` roda o
    `bd` no repo do workspace (detectado ou o de boot); `None` herda o cwd do
    processo do servidor."""
    try:
        proc = subprocess.run(
            ["bd", *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _http_post(url: str, payload: dict, timeout_s: float) -> tuple[int, str]:
    """(status, corpo). Status 0 = não respondeu. Cópia de vision.py:296-316."""
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    key = os.environ.get(KEY_ENV, "")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


def _http_get(url: str, timeout_s: float) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


def _popen(argv: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> int:
    """Sobe `argv` em segundo plano e devolve o pid. Segue
    `harness/backends/procs.py:191-214`: sessão nova para não deixar filho
    órfão, log aberto fora do `with` porque vive além desta função. `env`
    None herda o ambiente do servidor; `dispatch_do` pina config/data
    absolutos porque o filho roda com `cwd=workspace`, não o checkout."""
    fh = open(log, "w")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=fh,
            stderr=fh,
            start_new_session=True,
            env=env,
        )
    finally:
        fh.close()
    return proc.pid


# --------------------------------------------------------------------------- state readers


def status_text() -> str:
    """Um parágrafo por seção; seção que falha vira "(indisponível)" — fail
    -open porque isto só informa, nunca decide nada por conta própria."""
    linhas = []

    try:
        from harness import doctor

        result = doctor.checks()
        fails = doctor.failures(result)
        warns = [c for c in result if c.status == doctor.WARN]
        linhas.append(f"doctor: {len(fails)} falha(s), {len(warns)} aviso(s) de {len(result)}")
    except Exception:
        linhas.append("doctor: (indisponível)")

    try:
        from harness.ruler import selfapprove as ruler_selfapprove

        th = ruler_selfapprove.load_thresholds()
        linhas.append(
            f"auto-aprovação: enabled={th.enabled} external={th.external_enabled} "
            f"pinned={len(th.pinned)}"
        )
    except Exception:
        linhas.append("auto-aprovação: (indisponível)")

    try:
        from harness.improve import selfapprove as improve_selfapprove

        n = len(improve_selfapprove.queue_entries(limit=50))
        linhas.append(f"fila de aprovação: {n} proposta(s) esperando")
    except Exception:
        linhas.append("fila de aprovação: (indisponível)")

    linhas.append(jobs_text())
    return "\n".join(linhas)


def _queue_counts_for(project: str) -> tuple[int, int, int] | None:
    """`(fila, done, stuck)` do projeto registrado, ou None (não registrado
    ou registro ilegível) — fail-open, quem chama decide o texto sem
    contagem."""
    try:
        from harness import projects as harness_projects

        proj = harness_projects.load_projects().get(project)
        if proj is None:
            return None
        return harness_projects.queue_counts(proj)
    except Exception:
        return None


def ready_text(cwd: Path | None = None, *, limit: int = 10, project: str | None = None) -> str:
    rc, out = _bd("ready", cwd=cwd)
    if rc == 127:
        return "bd não instalado — não dá para listar tarefas"
    if rc != 0:
        if cwd is not None:
            # cwd explícito = repo de verdade (workspace detectado ou o
            # próprio boot do servidor via /ready): "sem beads aqui" é o caso
            # normal de um repo qualquer sem `.beads/`, não uma falha do
            # harness-core — nunca "bd ready falhou" pra esse canal.
            counts = _queue_counts_for(project) if project else None
            if counts is not None:
                fila, done, stuck = counts
                return (
                    f"sem beads em {cwd} — fila do harness: {fila} pendente(s), "
                    f"{done} done, {stuck} stuck"
                )
            return f"sem beads em {cwd} — fila do harness"
        first = (out.splitlines() or [""])[0]
        return f"bd ready falhou (rc={rc}): {first}"
    return "\n".join(out.splitlines()[:40])


def sa_queue_text(limit: int = 20) -> str:
    try:
        from harness.improve import selfapprove as improve_selfapprove

        entries = improve_selfapprove.queue_entries(limit=limit)
    except Exception:
        return "fila: (indisponível)"
    if not entries:
        return "fila: vazia"
    linhas = [
        f"{e.get('artifact', '?')} v{e.get('version', '?')}  id={e.get('id', '?')}"
        for e in entries
    ]
    return "\n".join(linhas)


def sa_history_text(limit: int = 10) -> str:
    try:
        from harness.improve import selfapprove as improve_selfapprove

        hist = improve_selfapprove.history(limit=limit)
    except Exception:
        return "histórico: (indisponível)"
    if not hist:
        return "histórico: vazio"
    linhas = [f"{m.mutation_id}  {m.rule_id}  {m.verdict}" for m in hist]
    return "\n".join(linhas)


def market_text(term: str) -> str:
    from harness.skills import market

    achados = market.search(term)[:20]
    if not achados:
        return f'market: nada casou com "{term}" (rode `harness market sync` na mão para atualizar)'
    linhas = [f"{e['id']:<32} {e['description'][:80]}" for e in achados]
    linhas.append(f"market: {len(achados)} achado(s)")
    return "\n".join(linhas)


def jobs_text(limit: int = 5) -> str:
    jobs = list_jobs()
    if not jobs:
        return "jobs: nenhum"
    running = sum(1 for j in jobs if job_running(j))
    linhas = [f"jobs: {running} rodando, {len(jobs) - running} terminado(s)"]
    for j in jobs[:limit]:
        estado = "rodando" if job_running(j) else "terminado"
        projeto = j.get("project") or "harness"
        linhas.append(f'  [{projeto}] {j.get("id")}  {estado}     "{j.get("task")}"  log: {j.get("log")}')
    return "\n".join(linhas)


# --------------------------------------------------------------------------- jobs


def jobs_dir() -> Path:
    d = store.data_dir()
    for part in JOBS_SUBDIR:
        d = d / part
    return d


def list_jobs() -> list[dict]:
    d = jobs_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in d.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda j: str(j.get("started_at", "")), reverse=True)
    return out


def job_running(job: dict) -> bool:
    pid = job.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def executor_argv(ex: Executor) -> list[str]:
    """Flags de rota pro `harness do`. Só lê do registro (`ex.backend`/
    `ex.run_model`) — texto de cliente nunca chega aqui, `resolve_executor` já
    é a única porta. Sem `--route`: `cmd_do` infere `manual` sozinho quando vê
    `--backend`/`--model` (harness/cli.py). Auto (`ex.backend == ""`) => []."""
    if not ex.backend:
        return []
    argv = ["--backend", ex.backend]
    if ex.run_model:
        argv += ["--model", ex.run_model]
    return argv


def dispatch_do(task: str, max_usd: float, ctx: ServeContext) -> dict:
    """Sobe `harness do <task> --no-apply --max-usd <max_usd>` em segundo plano e
    grava o registro do job. O teto viaja no argv — é o `cmd_do` (fail-closed,
    pré-dispatch) quem o aplica de verdade agora, não só o `pressure.cost_cap_usd`
    do governor (ambiente, fail-open). Não checa teto/concorrência — quem chama
    (`handle_message`) já decidiu que pode disparar.

    `ctx.executor` (pin do campo `model`) vira flags de rota via
    `executor_argv` — pin FORÇA `--backend`/`--model` e por isso DESLIGA a
    escalada automática de tier do `cmd_do` (`_proximo_tier` só entra sem
    pin): pinar local nunca escala pra pago, mas LM Studio caído + pin local
    é run que falha em vez de subir.

    O filho pina `CONFIG_DIR_ENV`/`DATA_DIR_ENV` absolutos no próprio env: sem
    isso, `paths.config_dir()` (relativo "config" do checkout) resolveria
    contra `cwd=ctx.cwd` (o workspace, não o checkout) e `pin_home_paths()`
    acabaria escrevendo em `~/.harness/config/projects.toml` — registro
    diferente do que o servidor está usando, re-registro silencioso."""
    ex = ctx.executor or auto_executor()
    jid = uuid.uuid4().hex[:8]
    d = jobs_dir()
    d.mkdir(parents=True, exist_ok=True)
    log = (d / f"{jid}.log").resolve()
    argv = [
        sys.executable,
        "-m",
        "harness.cli",
        "do",
        task,
        "--no-apply",
        "--max-usd",
        f"{max_usd:.2f}",
        *executor_argv(ex),
    ]
    env = {
        **os.environ,
        paths.CONFIG_DIR_ENV: str(paths.config_dir().resolve()),
        paths.DATA_DIR_ENV: str(store.data_dir().resolve()),
    }
    pid = _popen(argv, cwd=ctx.cwd, log=log, env=env)
    record = {
        "id": jid,
        "pid": pid,
        "task": task,
        "cwd": str(ctx.cwd),
        "project": ctx.project,
        "log": str(log),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now())),
        "max_usd": max_usd,
        "argv": argv,
        "executor": ex.id,
    }
    (d / f"{jid}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


# --------------------------------------------------------------------------- leitores de repo arbitrário


def _git(repo: Path, *args: str, timeout_s: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    """`git -C <repo> <args>` → (rc, stdout+stderr). Argv fixo, nunca shell —
    mesma convenção de `_bd` acima, mas para um repo qualquer (o workspace
    detectado), não o cwd do servidor."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def git_state_text(repo: Path) -> str:
    """Blocos independentes, cada um fail-open (doutrina de `status_text`):
    branch, HEAD, sujeira, últimos commits, entregas do harness. Repo
    não-git vira uma linha só — isto é o que o Cursor NÃO vê, não um `git
    status` completo."""
    rc, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return "não é repo git"
    linhas: list[str] = []
    rc, out = _git(repo, "branch", "--show-current")
    linhas.append(f"branch: {out.strip() or '(detached)'}" if rc == 0 else "branch: (indisponível)")
    rc, out = _git(repo, "log", "-1", "--date=short", "--pretty=%h %ad %s")
    linhas.append(f"HEAD: {out.strip()}" if rc == 0 and out.strip() else "HEAD: (indisponível)")
    rc, out = _git(repo, "status", "--porcelain")
    if rc == 0:
        arquivos = out.splitlines()
        linhas.append(f"sujo: {len(arquivos)} arquivo(s)")
        linhas.extend(arquivos[:15])
    else:
        linhas.append("sujo: (indisponível)")
    rc, out = _git(repo, "log", "-5", "--oneline", "--no-decorate")
    linhas.append("últimos commits:")
    linhas.extend(out.splitlines() if rc == 0 else ["(indisponível)"])
    rc, out = _git(repo, "branch", "--list", "harness/*")
    entregas = [b.strip().lstrip("* ").strip() for b in out.splitlines() if b.strip()] if rc == 0 else []
    linhas.append("entregas do harness:")
    linhas.extend(entregas[-5:] if entregas else ["(nenhuma)"])
    return "\n".join(linhas)


def readme_head(repo: Path) -> str:
    """Primeiro README que existir, bytes-bounded: 8KiB lidos, decode
    tolerante, 30 primeiras linhas, clipado em 1200 chars. `is_relative_to`
    depois do `resolve()` fecha escape por symlink (`repo` já chega
    resolvido, é o `det.path` da validação)."""
    for name in README_NAMES:
        p = repo / name
        try:
            if not p.is_file() or not p.resolve().is_relative_to(repo):
                continue
            texto = p.read_bytes()[:README_MAX_BYTES].decode("utf-8", "replace")
            linhas = texto.splitlines()[:README_MAX_LINES]
            return _clip("\n".join(linhas), README_MAX_CHARS)
        except OSError:
            return ""
    return ""


def tree_head(repo: Path) -> str:
    """Só `repo.iterdir()`, sem recursão — o modelo do Cursor já enxerga os
    arquivos do workspace; isto é um resumo raso do que ele NÃO vê, não uma
    busca de arquivo."""
    try:
        entradas = sorted(repo.iterdir(), key=lambda p: p.name)
    except OSError:
        return "(indisponível)"
    nomes = [
        f"{p.name}/" if p.is_dir() else p.name
        for p in entradas
        if not p.name.startswith(".") or p.name in (".beads", ".harness")
    ]
    if len(nomes) > TREE_MAX_ENTRIES:
        nomes = [*nomes[:TREE_MAX_ENTRIES], "…"]
    return ", ".join(nomes)


def _project_section(det: Detection) -> str:
    """Fila, marcos e custo acumulado do projeto registrado — só chamado
    quando `det.project` está setado; sem registro não há nada aqui pra
    somar."""
    linhas: list[str] = []
    try:
        from harness import projects as harness_projects

        proj = harness_projects.load_projects().get(det.project)
        if proj is None:
            linhas.append("fila do projeto: (não registrado)")
        else:
            fila, done, stuck = harness_projects.queue_counts(proj)
            linhas.append(f"fila do projeto: {fila} pendente(s), {done} done, {stuck} stuck")
            for nome, feitas, total in harness_projects.milestone_progress(proj)[:8]:
                linhas.append(f"  marco {nome}: {feitas}/{total}")
    except Exception:
        linhas.append("fila do projeto: (indisponível)")
    try:
        if store.db_path().is_file():
            hist = store.history(project=det.project, limit=100_000)
            usd = sum(r.cost_usd or 0.0 for r in hist)
            linhas.append(f"runs={len(hist)} usd={usd:.2f}")
    except Exception:
        linhas.append("runs: (indisponível)")
    return "\n".join(linhas)


def _jobs_for_workspace(det: Detection) -> str:
    """Jobs disparados NESTE workspace (cwd do record == repo detectado), até
    3 — `jobs_text()` já cobre o panorama global de todos os workspaces."""
    jobs = [j for j in list_jobs() if j.get("cwd") == str(det.path)][:3]
    if not jobs:
        return "jobs neste workspace: nenhum"
    linhas = ["jobs neste workspace:"]
    for j in jobs:
        estado = "rodando" if job_running(j) else "terminado"
        linhas.append(f'  {j.get("id")}  {estado}  "{j.get("task")}"')
    return "\n".join(linhas)


def workspace_state_text(det: Detection) -> str:
    """O que o modelo do Cursor NÃO vê: git/README/árvore do repo detectado,
    fila do harness se registrado, jobs disparados nele. Sem árvore
    recursiva, sem trecho de fonte, sem busca de arquivo — isso o editor já
    mostra. Seções independentes, cada uma em try/except (doutrina de
    `status_text`): uma falhando não derruba as outras."""
    assert det.path is not None
    secoes: list[Callable[[], str]] = [
        lambda: f"projeto: {det.project or '(não registrado no harness)'} · repo: {det.path}",
        lambda: git_state_text(det.path),  # type: ignore[arg-type]
        lambda: f"arquivos na raiz: {tree_head(det.path)}",  # type: ignore[arg-type]
        lambda: "README:\n" + readme_head(det.path),  # type: ignore[arg-type]
    ]
    if det.project:
        secoes.append(lambda: _project_section(det))
    secoes.append(lambda: _jobs_for_workspace(det))
    partes = []
    for fn in secoes:
        try:
            partes.append(_clip(fn()))
        except Exception:
            partes.append("(indisponível)")
    return "\n\n".join(partes)


# --------------------------------------------------------------------------- LLM path


def llm_base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def _chat_model() -> str:
    try:
        from harness.queue import DEFAULT_MODEL

        return DEFAULT_MODEL
    except Exception:
        return "openai:qwen3.5-9b-mlx"


def llm_available() -> bool:
    status, _ = _http_get(f"{llm_base_url()}/models", PROBE_TIMEOUT_S)
    return status == 200


def system_prompt(cwd: Path, detection: Detection | None = None) -> str:
    if detection is None or detection.path is None:
        # texto de hoje, byte-idêntico: nenhum workspace do Cursor foi
        # aceito nesta request (inclui quem ainda chama `system_prompt(cwd)`
        # com um argumento só).
        base = (
            "Você é o harness, o agente de engenharia que roda na máquina do Renan.\n"
            f"Plugado em {cwd} — você é o canal de controle SÓ deste repo (harness-core);\n"
            "não enxerga o projeto/workspace aberto no editor do usuário. Se perguntarem\n"
            'sobre "este projeto"/"meu projeto"/o código aberto e o contexto sugerir que\n'
            "não é este repo, diga que não vê o workspace do editor e aponte os comandos\n"
            "com barra abaixo ou os modelos nativos do Cursor — nunca chute sobre outro\n"
            "projeto.\n"
            "Responda em português, curto (no máximo 8 linhas). Nesta conversa você NÃO\n"
            "executa nada: para agir, o usuário usa os comandos com barra listados abaixo.\n\n"
            f"{HELP}"
        )
        state = _clip(status_text())
        ready = _clip(ready_text())
        block = trust_boundary.build_untrusted_block({"estado": state, "tarefas": ready})
        if block is not None:
            return f"{base}\n\n{block}"
        if not trust_boundary.enabled():
            extra = "\n\n".join(
                f"## {nome}\n{trust_boundary.sanitize(txt)}"
                for nome, txt in (("estado", state), ("tarefas", ready))
                if txt.strip()
            )
            return f"{base}\n\n{extra}" if extra else base
        return base

    # Workspace detectado: persona trocada — o modelo sabe que está falando
    # do projeto aberto no Cursor, não do harness-core. `status_text()`
    # (doctor, auto-aprovação, fila de aprovação — tudo estado do
    # harness-core) fica de fora deste ramo de propósito.
    det = detection
    frase = (
        f"Você está falando do projeto aberto no Cursor: {det.path} "
        f"({det.project or 'ainda não registrado no harness'}). /do, /ready, /new e "
        "/close valem NESTE repo; /queue, /history e /market são globais do harness."
    )
    try:
        n_rodando = sum(1 for j in list_jobs() if job_running(j))
        jobs_linha = f"\nharness: {n_rodando} job(s) rodando"
    except Exception:
        jobs_linha = ""
    base = (
        "Você é o harness, mas nesta conversa o contexto é outro projeto —\n"
        f"{frase}\n"
        "Responda em português, curto (no máximo 8 linhas). Nesta conversa você NÃO\n"
        "executa nada: para agir, o usuário usa os comandos com barra listados abaixo.\n\n"
        f"{HELP}{jobs_linha}"
    )
    state = _clip(workspace_state_text(det))
    ready = _clip(ready_text(det.path, project=det.project))
    block = trust_boundary.build_untrusted_block({"projeto": state, "tarefas": ready})
    if block is not None:
        return f"{base}\n\n{block}"
    if not trust_boundary.enabled():
        extra = "\n\n".join(
            f"## {nome}\n{trust_boundary.sanitize(txt)}"
            for nome, txt in (("projeto", state), ("tarefas", ready))
            if txt.strip()
        )
        return f"{base}\n\n{extra}" if extra else base
    return base


def _clip(text: str, limit: int = STATE_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (cortado)"


def chat_model_candidates() -> list[str]:
    """Nomes a tentar no endpoint OpenAI local, em ordem, sem duplicata.
    [0] = `_chat_model()` (comportamento de HOJE, byte-idêntico — inclui o
    `removeprefix(OPENAI_PREFIX)` que `llm_reply` já fazia); [1], se houver, é
    o `run_model` do primeiro executor LOCAL do registro que tiver um.

    Bug conhecido, não feature: `harness.queue.DEFAULT_MODEL` ==
    "openai:qwen3.5-9b-mlx" enquanto `config/models.toml` t0 ==
    "openai:qwen/qwen3.5-9b" — os dois nomes vivem no repo. `_chat_model()`
    continua primário (comportamento de hoje intocado); o segundo candidato é
    só o retry que cobre esse desalinhamento sem esperar ele ser corrigido."""
    candidates = [str(_chat_model()).removeprefix(OPENAI_PREFIX)]
    try:
        local = next((e for e in executors() if e.local and e.run_model), None)
    except Exception:
        local = None
    if local is not None:
        model = local.run_model.removeprefix(OPENAI_PREFIX)
        if model not in candidates:
            candidates.append(model)
    return candidates


def llm_reply(text: str, ctx: ServeContext) -> str | None:
    for model in chat_model_candidates():
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt(ctx.cwd, ctx.detection)},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            "max_tokens": 700,
        }
        status, body = _http_post(f"{llm_base_url()}/chat/completions", payload, LLM_TIMEOUT_S)
        if status == 200:
            try:
                data = json.loads(body)
                content = data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                return None
            if not isinstance(content, str) or not content.strip():
                return None
            return content
        if 400 <= status < 500:
            continue  # nome de modelo errado: tenta o próximo candidato
        return None  # 0/5xx/timeout: falha de infra, retry dobraria os 120s


# --------------------------------------------------------------------------- router


def route_hint(text: str, ctx: ServeContext) -> str:
    """1 linha determinística, ou "" — sem LLM, sem histórico, sem I/O de DB.
    Recebe o texto JÁ desembrulhado (saída de `user_query_text`): o bloco
    `<user_info>` polui `_FILE_RE` e as keywords do classificador.

    Só a HIPÓTESE DE PARTIDA (`[router.kind]` ou `default_tier`) — nunca o
    prior nem a escalada por tentativa, que dependem do ledger e isto aqui
    roda em todo turno de chat sem tocar banco. Guarda-corpo LOAD-BEARING:
    `[precedence].fallback = "code"` e `[router.kind].code = "t1"` (pago) —
    sem checar se a classificação caiu no fallback, todo papo solto ganharia
    aviso de tier pago."""
    try:
        from harness.routing.kinds import classify_kind
        from harness.routing.router import load_config, tier_by_name
        from harness.types import UnitSpec

        unit = UnitSpec(id="chat", path=ctx.cwd, prompt=text, verify_cmd="")
        kind, reasons = classify_kind(unit)
        if any(r.startswith("fallback:") for r in reasons):
            return ""
        cfg = load_config()
        tier_name = cfg["router"]["kind"].get(kind, cfg["router"]["default_tier"])
        tier = tier_by_name(cfg, tier_name)
        if _runs_local(tier.backend, tier.model):
            return ""
        alias = _tier_alias(tier, set())
        return (
            f"router: isso parece {kind} → o /do começaria em {alias} "
            f"({tier.backend}); o router pode subir de tier."
        )
    except Exception:
        return ""


def chat_notes(ex: Executor, text: str, ctx: ServeContext) -> list[str]:
    """Linhas extras antes da resposta do modelo local. Pin pago: avisa que
    quem respondeu foi o local mesmo, $0 (nunca gasta o executor pago num
    turno de chat). Auto: só o `route_hint`, e só quando ele tiver algo a
    dizer. Pin local: silêncio — o usuário escolheu, nada a acrescentar."""
    if ex.local is False:
        return [
            f"[{ex.id} é executor de /do — este turno foi respondido pelo modelo local, $0. "
            "Para usá-lo de verdade: /do <pedido>]"
        ]
    if ex.id == MODEL_ID:
        hint = route_hint(text, ctx)
        return [hint] if hint else []
    return []


HELP = """\
comandos do harness:
  /status                      — doctor, auto-aprovação e jobs em andamento
  /ready                       — tarefas prontas (bd ready)
  /queue                       — propostas esperando você (selfapprove queue)
  /history                     — decisões já tomadas (selfapprove history)
  /market <termo>               — procura skill no marketplace (só leitura)
  /new <título>                — cria tarefa (bd create)
  /close <id>                  — fecha tarefa (bd close)
  /do <pedido> [--max-usd N]   — roda `harness do` em segundo plano (teto 5.00)
  /where                        — workspace detectado no payload do Cursor (raw + veredito)
  /models                       — executores disponíveis (id pro campo "model" do cliente)
  /help                         — esta lista
texto sem barra vai para o modelo local (LM Studio, porta 1234) — chat é sempre local e $0.
"""


def _cmd_help(_arg: str, _ctx: ServeContext) -> str:
    return HELP


def _cmd_status(_arg: str, ctx: ServeContext) -> str:
    det = ctx.detection
    if det is not None and det.path is not None:
        return f"{workspace_state_text(det)}\n\n{jobs_text()}"
    return status_text()


def _cmd_ready(_arg: str, ctx: ServeContext) -> str:
    return ready_text(ctx.cwd, project=ctx.project)


def _global_prefix(ctx: ServeContext, text: str) -> str:
    """/queue, /history e /market são globais do harness-core mesmo com um
    workspace detectado — o prefixo deixa isso explícito pra não parecer que
    a fila é a do projeto aberto no Cursor."""
    det = ctx.detection
    if det is not None and det.path is not None:
        return f"(harness-core — global)\n{text}"
    return text


def _cmd_queue(_arg: str, ctx: ServeContext) -> str:
    return _global_prefix(ctx, sa_queue_text())


def _cmd_history(_arg: str, ctx: ServeContext) -> str:
    return _global_prefix(ctx, sa_history_text())


def _cmd_market(arg: str, ctx: ServeContext) -> str:
    term = arg.strip()
    if not term:
        return "uso: /market <termo>"
    return _global_prefix(ctx, market_text(term))


def _workspace_prefix(ctx: ServeContext, text: str) -> str:
    det = ctx.detection
    if det is not None and det.path is not None:
        nome = det.project or det.path.name
        return f"[{nome}] {text}"
    return text


def _cmd_new(arg: str, ctx: ServeContext) -> str:
    title = arg.strip()
    if not title:
        return "uso: /new <título>"
    rc, out = _bd("create", title, cwd=ctx.cwd)
    if rc == 127:
        return "bd não instalado — não dá para criar tarefa"
    first = (out.splitlines() or [""])[0]
    if rc != 0:
        return f"bd create falhou (rc={rc}): {first}"
    return _workspace_prefix(ctx, f"criada: {first}")


def _cmd_close(arg: str, ctx: ServeContext) -> str:
    jid = arg.strip()
    if not jid:
        return "uso: /close <id>"
    rc, out = _bd("close", jid, cwd=ctx.cwd)
    if rc == 127:
        return "bd não instalado — não dá para fechar tarefa"
    first = (out.splitlines() or [""])[0]
    if rc != 0:
        return f"bd close falhou (rc={rc}): {first}"
    return _workspace_prefix(ctx, f"fechada: {first}")


_MAX_USD_RE = re.compile(r"--max-usd\s+(\S+)")


def _cmd_do(arg: str, ctx: ServeContext) -> str:
    m = _MAX_USD_RE.search(arg)
    task = (_MAX_USD_RE.sub("", arg)).strip()
    max_usd = MAX_USD_CAP
    if m:
        raw = m.group(1)
        try:
            max_usd = float(raw)
        except ValueError:
            return f'--max-usd precisa de um número, veio "{raw}"'
    if not task:
        return "uso: /do <pedido> [--max-usd N]"
    if max_usd <= 0:
        return "recusado: --max-usd tem que ser maior que zero. Nada foi disparado."
    if max_usd > MAX_USD_CAP:
        return (
            f"recusado: --max-usd {max_usd:.2f} passa do teto do servidor "
            f"({MAX_USD_CAP:.2f}). Nada foi disparado."
        )
    running = [j for j in list_jobs() if job_running(j)]
    if len(running) >= MAX_RUNNING_JOBS:
        jid = running[0].get("id")
        return f"já tem um job rodando ({jid}) — espere terminar (/status). Nada foi disparado."

    record = dispatch_do(task, max_usd, ctx)
    # `--max-usd` agora viaja no argv e é aplicado fail-closed pelo `cmd_do`
    # (`ceiling`), não só pelo `pressure.cost_cap_usd` do governor (ambiente,
    # fail-open) — a linha do teto reflete isso, sem alarme de "SEM TETO".
    cap = load_gov().cost_cap_usd
    extra = f" · teto ambiente do governor: ${cap:.2f}" if cap > 0 else ""
    teto_linha = f"teto deste run: ${max_usd:.2f} (--max-usd, vale para todas as tentativas){extra}"
    linhas = [
        f"job {record['id']} iniciado em {ctx.cwd}",
        f"pedido: {task}",
        teto_linha,
        "--no-apply: o resultado fica na branch de entrega; nada é mergeado sozinho",
    ]
    ex = ctx.executor or auto_executor()
    if ex.id != MODEL_ID:
        pin_linha = (
            f"executor pinado: {ex.id} ({ex.backend} · {ex.run_model or 'modelo do backend'}) "
            "— sem escalada automática de tier"
        )
        if not ex.local:
            pin_linha += f" · PAGO, teto ${max_usd:.2f}"
        linhas.append(pin_linha)
    linhas.append(f"acompanhe com /status · log: {record['log']}")
    resp = "\n".join(linhas)
    det = ctx.detection
    if det is not None and det.path is not None:
        projeto_linha = (
            f"projeto: {det.project}"
            if det.project
            else "projeto: não registrado — o harness registra na primeira entrega"
        )
        resp = f"{resp}\n{projeto_linha}"
    return resp


def _cmd_where(_arg: str, ctx: ServeContext) -> str:
    """Superfície de aceite da detecção inteira: mostra exatamente o que foi
    lido do payload, o veredito, as roots ativas e onde o `/do` rodaria —
    para o drift do formato do Cursor ficar visível em vez de silencioso."""
    det = ctx.detection
    raw = det.raw if det is not None else None
    veredito = det.verdict if det is not None else "ausente"
    roots = allowed_roots(ctx)
    roots_txt = ", ".join(str(r) for r in roots) if roots else "(nenhuma)"
    nome = det.project if det is not None and det.project else "não registrado"
    return "\n".join(
        [
            f"payload extraído: {raw if raw is not None else 'nenhum <user_info> no payload'}",
            f"veredito: {veredito}",
            f"roots permitidas: {roots_txt}",
            f"projeto: {nome}",
            f"o /do rodaria em: {ctx.cwd}",
            f"executor pedido: {ctx.requested_model or '(nenhum)'} → "
            f"{ctx.executor.id if ctx.executor else MODEL_ID}",
        ]
    )


def _cmd_models(_arg: str, ctx: ServeContext) -> str:
    """Lista os executores do registro (mesmos ids aceitos no campo `model`)
    — inclusive quando um deles está pinado nesta conversa, marcado com "→"."""
    pin = ctx.executor.id if ctx.executor else None
    linhas = [
        f"{'→ ' if e.id == pin else '  '}{e.id:<22} "
        f"{'chat + /do' if e.local else 'só /do (pago)'}  {e.backend or 'router'} · {e.run_model or '—'}"
        for e in executors()
    ]
    linhas.append("chat é sempre local ($0); executor pago só executa via /do (teto $5.00).")
    return "\n".join(linhas)


_COMMANDS: dict[str, Callable[[str, ServeContext], str]] = {
    "help": _cmd_help,
    "status": _cmd_status,
    "ready": _cmd_ready,
    "queue": _cmd_queue,
    "history": _cmd_history,
    "market": _cmd_market,
    "new": _cmd_new,
    "close": _cmd_close,
    "do": _cmd_do,
    "where": _cmd_where,
    "models": _cmd_models,
}


def handle_message(text: str, ctx: ServeContext) -> str:
    stripped = text.strip()
    if stripped.startswith("/"):
        rest = stripped[1:]
        name, _, arg = rest.partition(" ")
        name = name.strip().lower()
        handler = _COMMANDS.get(name)
        if handler is None:
            return f"comando desconhecido: /{name}\n\n{HELP}"
        return handler(arg, ctx)
    ex = ctx.executor or auto_executor()
    notes = chat_notes(ex, text, ctx)
    reply = llm_reply(text, ctx)
    if reply is None:
        msg = (
            f"LM Studio não respondeu em {llm_base_url()} — sem modelo local eu só "
            f"respondo comando:\n\n{HELP}"
        )
        return "\n".join([*notes, msg])
    return "\n".join([*notes, reply])


def last_user_text(messages: list[dict]) -> str:
    """Última mensagem `role=="user"`, com o shape str-ou-partes resolvido
    por `message_text` e o `<user_query>` do Cursor desembrulhado por
    `user_query_text` — sem isso todo slash command digitado no Cursor chega
    embrulhado em `<timestamp>...</timestamp>\\n<user_query>\\n/status\\n</user_query>`
    e o `startswith("/")` do router nunca bate."""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        return user_query_text(message_text(msg))
    return ""


# --------------------------------------------------------------------------- OpenAI shapes


def new_id(rng: Callable[[], uuid.UUID] = uuid.uuid4) -> str:
    return f"chatcmpl-{rng().hex[:24]}"


def models_payload() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": e.id, "object": "model", "created": 0, "owned_by": "harness"} for e in executors()
        ],
    }


def model_error_payload(requested: str | None) -> dict:
    """Shape de erro OpenAI pro `model` desconhecido — `param`/`code` são o
    que o Cursor (e qualquer cliente decente) usa pra distinguir isto de
    qualquer outro 4xx. `requested` sanitizado (sem control chars, clip em
    `MAX_MODEL_ID_CHARS`): é texto de cliente indo pra uma mensagem de erro,
    mesma cautela de `_safe_for_log`."""
    safe = re.sub(r"[\x00-\x1f\x7f]", "?", requested or "")[:MAX_MODEL_ID_CHARS]
    ids = [e.id for e in executors()]
    return {
        "error": {
            "message": f"modelo desconhecido: {safe!r}. Disponíveis: {', '.join(ids)}",
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_found",
        }
    }


def completion_payload(text: str, *, cid: str, created: int, model: str = MODEL_ID) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _chunk_frame(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"


def stream_chunks(text: str, *, cid: str, created: int, model: str = MODEL_ID) -> Iterator[bytes]:
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
    yield _chunk_frame(
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
    )
    for i in range(0, len(text), CHUNK_CHARS):
        pedaco = text[i : i + CHUNK_CHARS]
        yield _chunk_frame(
            {**base, "choices": [{"index": 0, "delta": {"content": pedaco}, "finish_reason": None}]}
        )
    yield _chunk_frame({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    yield b"data: [DONE]\n\n"


# --------------------------------------------------------------------------- auth

NO_KEY_HELP = (
    f"[serve] SEM API KEY fora do loopback: recusando tudo com {FORBIDDEN}. Para ligar, "
    f"passe --api-key ou exporte {API_KEY_ENV}=<segredo>; o cliente manda o mesmo valor "
    'no header Authorization: "Bearer <segredo>".'
)


def is_loopback(host: str) -> bool:
    """127.0.0.1/localhost/::1 são a mesma máquina; qualquer outro host expõe na rede."""
    return host in LOOPBACK_HOSTS


def _bearer(raw: str | None) -> str | None:
    """`Authorization: Bearer <token>` → token; formato torto vira None."""
    if not raw or not raw.startswith("Bearer "):
        return None
    return raw[len("Bearer ") :]


def key_ok(api_key: str | None, presented: str | None) -> bool:
    """Comparação em tempo constante. Sem key configurada: sempre False —
    quem chama decide se isso barra (fora do loopback) ou é irrelevante
    (loopback sem key, comportamento de hoje)."""
    if not api_key:
        return False
    return hmac.compare_digest(api_key.encode("utf-8"), (presented or "").encode("utf-8"))


def auth_status(api_key: str | None, require_auth: bool, authorization_header: str | None) -> int | None:
    """Porteiro puro, mesmo desenho de `webhook.screen_request`: `None` deixa
    passar, senão devolve o status HTTP do refuso.

    `require_auth` é True com key configurada OU host fora do loopback (a
    regra fail-closed do webhook). Loopback sem key: `require_auth=False` e a
    função sempre deixa passar — mantém o comportamento de hoje intacto.
    """
    if not require_auth:
        return None
    if not api_key:
        return FORBIDDEN
    return None if key_ok(api_key, _bearer(authorization_header)) else UNAUTHORIZED


# --------------------------------------------------------------------------- handler + server


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "harness-serve"

    @property
    def ctx(self) -> ServeContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        """Porta de entrada de toda rota — chamada antes de olhar o path ou
        ler o corpo. `status is None` deixa passar; senão já escreve a
        resposta (401 key errada/faltando, 403 fail-closed sem key fora do
        loopback) e devolve False."""
        status = auth_status(
            self.server.api_key,  # type: ignore[attr-defined]
            self.server.require_auth,  # type: ignore[attr-defined]
            self.headers.get("Authorization"),
        )
        if status is None:
            return True
        # corpo (se houver) nunca é lido: quem não autenticou não dita o
        # framing da próxima request na mesma conexão keep-alive.
        self.close_connection = True
        if status == UNAUTHORIZED:
            self._json(UNAUTHORIZED, {"error": {"message": "invalid api key", "type": "invalid_request_error"}})
        else:
            self._error(status, f"sem api key configurada — host fora do loopback exige --api-key ou {API_KEY_ENV}")
        return False

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._json(200, models_payload())
            return
        if path in ("", "/health", "/v1/health"):
            self._json(
                200,
                {
                    "status": "ok",
                    "model": MODEL_ID,
                    "cwd": str(self.ctx.cwd),
                    "models": [e.id for e in executors()],
                },
            )
            return
        self._error(404, f"rota desconhecida: {self.path}")

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, f"rota desconhecida: {self.path}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            # corpo nunca é lido: 100+ mensagens com tool result é entrada de
            # cliente sem teto, e keep-alive não pode herdar o framing de
            # quem mandou um corpo gigante.
            self.close_connection = True
            self._error(PAYLOAD_TOO_LARGE, f"corpo maior que {MAX_BODY_BYTES} bytes")
            return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw or b"{}")
        except (ValueError, OSError) as exc:
            self._error(400, f"corpo inválido: {exc}")
            return
        if not isinstance(body, dict):
            self._error(400, "corpo precisa ser um objeto JSON")
            return
        # `tools`/`stream_options` seguem ignorados — non-goals: nunca
        # emitimos tool_calls nem usage chunk, e o Cursor aceita numa boa
        # (não é gramática que ele exige para funcionar). `model` agora
        # escolhe o executor — resolvido ANTES do stream: 404 é
        # irrepresentável no meio de um SSE, e o Cursor manda `stream=True`.
        requested = body.get("model") if isinstance(body.get("model"), str) else None
        ex = resolve_executor(requested)
        degradado = False
        if ex is None:
            if os.environ.get(STRICT_MODEL_ENV) == "0":
                ex = auto_executor()
                degradado = True
                print(
                    f"[serve] modelo desconhecido ({_safe_for_log(requested or '')}) — "
                    f"degradando pra auto ({STRICT_MODEL_ENV}=0)",
                    file=sys.stderr,
                )
            else:
                self._json(MODEL_NOT_FOUND, model_error_payload(requested))
                return
        messages = body.get("messages") or []
        req_ctx = replace(ctx_for_request(messages, self.ctx), executor=ex, requested_model=requested)
        text = handle_message(last_user_text(messages), req_ctx)
        cid = new_id()
        created = int(time.time())
        # Eco o que o cliente pediu, não sempre o id canônico — mas só quando
        # bate EXATO com o que resolveu (id primário ou alias oculto): a
        # resolução é case-insensitive (`resolve_executor`), e ecoar de volta
        # uma grafia torta ("HARNESS:LOCAL") devolveria um `model` que não
        # está em `models_payload()` nem bate nenhum `e.id` de verdade —
        # nesse caso o eco cai pro id canônico do que resolveu, não pro auto.
        echo = ex.id
        if not degradado and requested:
            key = requested.strip()
            if key == ex.id or (ex.tier and key == f"{EXECUTOR_PREFIX}{ex.tier}"):
                echo = key
        if bool(body.get("stream")):
            self._stream(cid, created, text, model=echo)
        else:
            self._json(200, completion_payload(text, cid=cid, created=created, model=echo))

    def _stream(self, cid: str, created: int, text: str, *, model: str = MODEL_ID) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for frame in stream_chunks(text, cid=cid, created=created, model=model):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(frame), frame))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _raw(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._raw(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "harness_serve"}})

    def log_message(self, *args: object) -> None:
        pass  # silêncio: quem quer log é quem sobe o processo, não o handler


def _boot_workspace_roots(raw: Sequence[str | Path] | None) -> tuple[Path, ...]:
    """Precedência param CLI (`--workspace-root`, repetível) > env
    `HARNESS_SERVE_WORKSPACE_ROOTS` (split `os.pathsep`) > `default_workspace_roots()`.
    Cada candidato `expanduser().resolve()`, só quem sobrevive `is_dir()`
    entra na tupla — uma root que não existe não trava o boot, só fica fora
    da allowlist."""
    if raw:
        candidatos: list[str | Path] = list(raw)
    else:
        env = os.environ.get(WORKSPACE_ROOTS_ENV)
        candidatos = env.split(os.pathsep) if env else list(default_workspace_roots())
    roots: list[Path] = []
    for c in candidatos:
        try:
            p = Path(c).expanduser().resolve()
        except OSError:
            continue
        if p.is_dir():
            roots.append(p)
    return tuple(roots)


def serve(
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    *,
    cwd: Path | None = None,
    on_bind: Callable[[int], None] | None = None,
    max_requests: int | None = None,
    api_key: str | None = None,
    workspace_roots: Sequence[str | Path] | None = None,
) -> None:
    # Fail-closed do webhook: fora do loopback, key é obrigatória. Loopback
    # sem key segue exatamente como hoje — sem auth nenhuma.
    require_auth = bool(api_key) or not is_loopback(host)
    if require_auth and not api_key:
        print(NO_KEY_HELP, file=sys.stderr)
    roots = _boot_workspace_roots(workspace_roots)
    print(
        f"[serve] workspace roots: {', '.join(str(r) for r in roots) or '(nenhuma)'}",
        file=sys.stderr,
    )
    server = ThreadingHTTPServer((host, port), _Handler)
    server.ctx = ServeContext(  # type: ignore[attr-defined]
        cwd=Path(cwd or Path.cwd()).resolve(), workspace_roots=roots
    )
    server.api_key = api_key  # type: ignore[attr-defined]
    server.require_auth = require_auth  # type: ignore[attr-defined]
    try:
        if on_bind is not None:
            on_bind(server.server_address[1])
        if max_requests is None:
            server.serve_forever()
        else:
            for _ in range(max_requests):
                server.handle_request()
    finally:
        server.server_close()
