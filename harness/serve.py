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

A segurança do módulo é estrutural, não é uma checagem: `_COMMANDS` é um
dict FECHADO de comandos de leitura e de baixo risco (status, ready, queue,
history, market, new, close, do). Não existe caminho, daqui, para
`market approve`, `selfapprove approve/undo`, `seal` ou qualquer escrita em
`config/genome.toml` — quem quer aprovar algo continua tendo de usar a CLI
na mão. `/do` dispara em segundo plano com `--no-apply`: uma mensagem de
chat não merge sozinha na branch default.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from harness import trust_boundary
from harness.governor.governor import load_gov
from harness.ledger import store

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
MODEL_ID = "harness"
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


@dataclass(frozen=True)
class ServeContext:
    """Estado pinado no boot do servidor. `cwd` nunca é `Path.cwd()` do
    request — o repo que o `/do` usa é o que estava aberto quando o processo
    subiu, não o que estiver no ambiente da thread que atende a conexão."""

    cwd: Path
    now: Callable[[], float] = field(default=time.time)


# --------------------------------------------------------------------------- seams


def _bd(*args: str, timeout_s: float = BD_TIMEOUT_S) -> tuple[int, str]:
    """`bd <args>` → (rc, stdout+stderr). Indireção para o teste trocar por
    um fake, mesma convenção de `_http_post`/`_http_get` abaixo."""
    try:
        proc = subprocess.run(
            ["bd", *args], capture_output=True, text=True, timeout=timeout_s
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


def _popen(argv: list[str], *, cwd: Path, log: Path) -> int:
    """Sobe `argv` em segundo plano e devolve o pid. Segue
    `harness/backends/procs.py:191-214`: sessão nova para não deixar filho
    órfão, log aberto fora do `with` porque vive além desta função."""
    fh = open(log, "w")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=fh,
            stderr=fh,
            start_new_session=True,
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


def ready_text(limit: int = 10) -> str:
    rc, out = _bd("ready")
    if rc == 127:
        return "bd não instalado — não dá para listar tarefas"
    if rc != 0:
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
        linhas.append(f'  {j.get("id")}  {estado}     "{j.get("task")}"  log: {j.get("log")}')
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


def dispatch_do(task: str, max_usd: float, ctx: ServeContext) -> dict:
    """Sobe `harness do <task> --no-apply` em segundo plano e grava o
    registro do job. Não checa teto/concorrência — quem chama (`handle_message`)
    já decidiu que pode disparar."""
    jid = uuid.uuid4().hex[:8]
    d = jobs_dir()
    d.mkdir(parents=True, exist_ok=True)
    log = (d / f"{jid}.log").resolve()
    argv = [sys.executable, "-m", "harness.cli", "do", task, "--no-apply"]
    pid = _popen(argv, cwd=ctx.cwd, log=log)
    record = {
        "id": jid,
        "pid": pid,
        "task": task,
        "cwd": str(ctx.cwd),
        "log": str(log),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.now())),
        "max_usd": max_usd,
        "argv": argv,
    }
    (d / f"{jid}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


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


def system_prompt() -> str:
    base = (
        "Você é o harness, o agente de engenharia que roda na máquina do Renan.\n"
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


def _clip(text: str, limit: int = STATE_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (cortado)"


def llm_reply(text: str) -> str | None:
    model = str(_chat_model()).removeprefix(OPENAI_PREFIX)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    status, body = _http_post(f"{llm_base_url()}/chat/completions", payload, LLM_TIMEOUT_S)
    if status != 200:
        return None
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    return content


# --------------------------------------------------------------------------- router


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
  /help                        — esta lista
texto sem barra vai para o modelo local (LM Studio, porta 1234).
"""


def _cmd_help(_arg: str, _ctx: ServeContext) -> str:
    return HELP


def _cmd_status(_arg: str, _ctx: ServeContext) -> str:
    return status_text()


def _cmd_ready(_arg: str, _ctx: ServeContext) -> str:
    return ready_text()


def _cmd_queue(_arg: str, _ctx: ServeContext) -> str:
    return sa_queue_text()


def _cmd_history(_arg: str, _ctx: ServeContext) -> str:
    return sa_history_text()


def _cmd_market(arg: str, _ctx: ServeContext) -> str:
    term = arg.strip()
    if not term:
        return "uso: /market <termo>"
    return market_text(term)


def _cmd_new(arg: str, _ctx: ServeContext) -> str:
    title = arg.strip()
    if not title:
        return "uso: /new <título>"
    rc, out = _bd("create", title)
    if rc == 127:
        return "bd não instalado — não dá para criar tarefa"
    first = (out.splitlines() or [""])[0]
    if rc != 0:
        return f"bd create falhou (rc={rc}): {first}"
    return f"criada: {first}"


def _cmd_close(arg: str, _ctx: ServeContext) -> str:
    jid = arg.strip()
    if not jid:
        return "uso: /close <id>"
    rc, out = _bd("close", jid)
    if rc == 127:
        return "bd não instalado — não dá para fechar tarefa"
    first = (out.splitlines() or [""])[0]
    if rc != 0:
        return f"bd close falhou (rc={rc}): {first}"
    return f"fechada: {first}"


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
    cap = load_gov().cost_cap_usd
    if cap <= 0:
        teto_linha = (
            f"teto pedido: ${max_usd:.2f} · ATENÇÃO: o runner está SEM TETO "
            "(pressure.cost_cap_usd=0 em config/governor.toml) — o --max-usd "
            "não é aplicado por ele"
        )
    else:
        teto_linha = (
            f"teto pedido: ${max_usd:.2f} · teto real do runner "
            f"(governor cost_cap_usd): ${cap:.2f}"
        )
    return (
        f"job {record['id']} iniciado em {ctx.cwd}\n"
        f"pedido: {task}\n"
        f"{teto_linha}\n"
        "--no-apply: o resultado fica na branch de entrega; nada é mergeado sozinho\n"
        f"acompanhe com /status · log: {record['log']}"
    )


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
    reply = llm_reply(text)
    if reply is None:
        return (
            f"LM Studio não respondeu em {llm_base_url()} — sem modelo local eu só "
            f"respondo comando:\n\n{HELP}"
        )
    return reply


def last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "".join(parts)
        return ""
    return ""


# --------------------------------------------------------------------------- OpenAI shapes


def new_id(rng: Callable[[], uuid.UUID] = uuid.uuid4) -> str:
    return f"chatcmpl-{rng().hex[:24]}"


def models_payload() -> dict:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "harness"}],
    }


def completion_payload(text: str, *, cid: str, created: int) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": MODEL_ID,
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


def stream_chunks(text: str, *, cid: str, created: int) -> Iterator[bytes]:
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID}
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


# --------------------------------------------------------------------------- handler + server


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "harness-serve"

    @property
    def ctx(self) -> ServeContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._json(200, models_payload())
            return
        if path in ("", "/health", "/v1/health"):
            self._json(200, {"status": "ok", "model": MODEL_ID, "cwd": str(self.ctx.cwd)})
            return
        self._error(404, f"rota desconhecida: {self.path}")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, f"rota desconhecida: {self.path}")
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw or b"{}")
        except (ValueError, OSError) as exc:
            self._error(400, f"corpo inválido: {exc}")
            return
        if not isinstance(body, dict):
            self._error(400, "corpo precisa ser um objeto JSON")
            return
        text = handle_message(last_user_text(body.get("messages") or []), self.ctx)
        cid = new_id()
        created = int(time.time())
        if bool(body.get("stream")):
            self._stream(cid, created, text)
        else:
            self._json(200, completion_payload(text, cid=cid, created=created))

    def _stream(self, cid: str, created: int, text: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for frame in stream_chunks(text, cid=cid, created=created):
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


def serve(
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    *,
    cwd: Path | None = None,
    on_bind: Callable[[int], None] | None = None,
    max_requests: int | None = None,
) -> None:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.ctx = ServeContext(cwd=Path(cwd or Path.cwd()).resolve())  # type: ignore[attr-defined]
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
