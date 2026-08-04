"""Backend deepagents: o único arquivo do repo que conhece LangChain.

Risco 1 da SPEC — a API do deepagents é jovem. Todo import de
`deepagents`/`langchain`/`langgraph` mora aqui e é *lazy* (dentro do método),
para que `harness backends` funcione com o extra desinstalado.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, ClassVar, Sequence

from harness.skills import render_prompt, select_skills
from harness.types import Capabilities, ExecRequest, ExecResult, ExitReason, Preflight

try:
    from harness.backends.mcp_tools import load_mcp_tools
except Exception:  # módulo chega em PR paralelo; sem ele, sem tools extras
    load_mcp_tools = lambda *a, **k: []  # noqa: E731

OLLAMA_PREFIX = "ollama:"
OLLAMA_URL = "http://localhost:11434/api/tags"
OLLAMA_TIMEOUT_S = 2.0

CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"
MODELS_FILE = "models.toml"

# Sentinela do `ModelCallLimitMiddleware` (langchain 1.3.14,
# `_build_limit_exceeded_message`): é como o limite de turnos se anuncia, já que
# nada no retorno do grafo diz por que ele parou.
LIMIT_MESSAGE_PREFIX = "Model call limits exceeded:"

_INSTALL_HINT = "deepagents não instalado — pip install harness-core[deepagents]"

# Contrato com o prompt-builder: se este arquivo existir, seu conteúdo é
# prependado às instructions do agente; ausente => comportamento atual.
EXECUTOR_PROMPT_PATH = Path("prompts/executor.md")


class DeepagentsBackend:
    name: ClassVar[str] = "deepagents"

    def __init__(self, model: str | None = None) -> None:
        # `preflight()` do Protocol não recebe argumento, mas checar o modelo é
        # metade do valor do preflight aqui; quem sabe o modelo seta o atributo.
        self.model = model

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,  # checkpointer só entra no PR-2
            reports_cost=True,
            model_selectable=True,
            tools=frozenset(
                {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
            ),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        """Import + servidor local. ZERO chamada de LLM."""
        return self._preflight(self.model)

    def _preflight(self, model: str | None) -> Preflight:
        try:
            _import_deepagents()
        except ImportError as exc:
            return Preflight(ok=False, reason=f"{_INSTALL_HINT} ({exc})")
        if model and model.startswith(OLLAMA_PREFIX):
            return _ollama_preflight(model)
        return Preflight(ok=True, reason="deepagents importável")

    def execute(self, req: ExecRequest) -> ExecResult:
        _bootstrap_env()

        pre = self._preflight(req.model)
        if not pre.ok:
            return _failure(req, "blocked", pre.reason)

        before = _snapshot(req.workspace, exclude=req.trace_path)
        agent, usage_cb = _build_agent(req)
        tracer = _trace_collector()
        config: dict[str, Any] = {
            # `recursion_limit` conta passos do grafo (modelo + tools + middleware),
            # não turnos; folga de 4x para o limite real ser o middleware.
            "recursion_limit": max(50, req.max_turns * 4),
            "callbacks": [usage_cb, tracer],
        }

        payload = {"messages": [{"role": "user", "content": req.prompt}]}
        try:
            state = _with_timeout(lambda: agent.invoke(payload, config), req.timeout_s)
        except TimeoutError:
            # Cópia: a thread do invoke não morre no timeout e segue mutando a lista.
            partial = list(tracer.messages)
            _write_trace(req.trace_path, partial, error=f"timeout após {req.timeout_s}s")
            return _result(
                req,
                before,
                ok=False,
                exit_reason="timeout",
                turns=_turns(partial),
                usage=usage_cb,
            )
        except Exception as exc:  # inclui GraphRecursionError e erro de provider
            partial = list(tracer.messages)
            if type(exc).__name__ == "GraphRecursionError":
                _write_trace(req.trace_path, partial, error=f"GraphRecursionError: {exc}")
                return _result(
                    req,
                    before,
                    ok=False,
                    exit_reason="max_turns",
                    turns=_turns(partial),
                    usage=usage_cb,
                )
            return _failure(req, "error", f"{type(exc).__name__}: {exc}", messages=partial)

        messages = state.get("messages", [])
        _write_trace(req.trace_path, messages)
        after = _snapshot(req.workspace, exclude=req.trace_path)
        changed = _diff(before, after)
        turns = _turns(messages)
        hit_limit = _hit_call_limit(messages)
        exit_reason: ExitReason
        if hit_limit:
            exit_reason, ok = "max_turns", False
            turns = max(0, turns - 1)  # a mensagem-sentinela não é um turno do agente
        elif not changed and not _final_text(messages):
            # Desistência silenciosa: nada escrito e nada dito. "done" aqui é o
            # ledger registrando sucesso num run que não produziu nada.
            exit_reason, ok = "stalled", False
        else:
            exit_reason, ok = "done", True
        return _result(
            req, before, ok=ok, exit_reason=exit_reason, turns=turns, usage=usage_cb, after=after
        )


# --------------------------------------------------------------------------- ambiente


def _bootstrap_env() -> None:
    """Defensivo: o cli já faz isto, mas o backend pode ser chamado direto."""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")


def _import_deepagents():
    import deepagents

    return deepagents


# --------------------------------------------------------------------------- ollama


def _ollama_preflight(model: str) -> Preflight:
    tag = model[len(OLLAMA_PREFIX) :]
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=OLLAMA_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Preflight(ok=False, reason=f"servidor Ollama não respondeu em {OLLAMA_URL} ({exc})")
    names = {m.get("name", "") for m in payload.get("models", [])}
    names |= {n.removesuffix(":latest") for n in names}
    if tag not in names:
        listed = ", ".join(sorted(names)) or "nenhum"
        return Preflight(ok=False, reason=f"modelo {tag!r} ausente no Ollama (tem: {listed})")
    return Preflight(ok=True, reason=f"ollama ok, modelo {tag} presente")


# --------------------------------------------------------------------------- agente


def _build_agent(req: ExecRequest):
    from deepagents import create_deep_agent
    from deepagents.backends.local_shell import LocalShellBackend
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from langchain_core.callbacks import UsageMetadataCallbackHandler

    req.workspace.mkdir(parents=True, exist_ok=True)
    # LocalShellBackend herda o FilesystemBackend e ADICIONA `execute`: com o
    # FilesystemBackend puro o FilesystemMiddleware filtra a tool `execute`
    # antes do modelo em request-time (`FilesystemMiddleware.
    # _unsupported_tools_and_execution_state` → `supports_execution`, que é um
    # isinstance de SandboxBackendProtocol) — o agente nunca conseguia rodar o
    # verify_cmd, mesmo com a tool registrada no grafo.
    # O shell é real, mas o root/cwd é o workspace do run (worktree efêmero) e
    # virtual_mode=True continua bloqueando path traversal nas tools de arquivo.
    fs = LocalShellBackend(root_dir=str(req.workspace), virtual_mode=True)

    middleware: list[Any] = []
    allowed = _fs_allowlist(req.tools)
    if allowed:
        # `tools=` do create_deep_agent é aditivo; a única forma de restringir é
        # substituir o FilesystemMiddleware (merge por `.name`).
        from deepagents.middleware.filesystem import FilesystemMiddleware

        middleware.append(FilesystemMiddleware(backend=fs, tools=allowed))
    middleware.append(ModelCallLimitMiddleware(run_limit=req.max_turns, exit_behavior="end"))

    usage_cb = UsageMetadataCallbackHandler()
    # Convenção de workspace é responsabilidade do BACKEND, não da unit: o
    # filesystem virtual tem root no workspace, e a unit fala "seu diretório de
    # trabalho" sem saber disso. Sem esta ponte, modelo pequeno alucina path.
    # O "/" só existe para as tools de arquivo (filesystem virtual com root no
    # workspace); a tool `execute` é shell REAL com cwd no workspace, então
    # `ls /dist` lá cai na raiz da máquina e volta vazio — o modelo conclui que
    # a tarefa não tem arquivos e encerra sem escrever nada.
    system_prompt = (
        "Seu diretório de trabalho é o root do filesystem: os arquivos da "
        'tarefa estão em "/" (ex.: /arquivo.py). Use ls para conferir e as '
        "tools de arquivo (read_file, edit_file, write_file) para mexer neles. "
        "Na tool `execute` é diferente: é shell real e o cwd JÁ é o diretório "
        'de trabalho, então use path RELATIVO ("dist/", "./x.py") — "/" no '
        "shell é a raiz da máquina e não tem nada da tarefa. "
        # read_file numera as linhas; modelo pequeno copia a numeração/indentação
        # de volta no old_string e o edit_file (match exato) falha em loop.
        "O `read_file` mostra números de linha que NÃO existem no arquivo: no "
        "`old_string` do edit_file use só o texto cru, com a indentação exata. "
        "Se o edit_file falhar duas vezes com 'String not found', pare de "
        "tentar e reescreva o arquivo inteiro com write_file."
    )
    # `kind` já é campo do ExecRequest (preenchido pelo router no run_graph e
    # pela unit no cli); getattr segura request serializado de genoma antigo.
    skills = select_skills(getattr(req, "kind", None))
    skills_block = render_prompt(skills)
    if skills_block:
        system_prompt = f"{system_prompt}\n\n{skills_block}"
    _record_skill_usage(req, skills)

    recall_block = _episodic_block(getattr(req, "kind", None), req.prompt)
    if recall_block:
        system_prompt = f"{system_prompt}\n\n{recall_block}"

    base_prompt = _executor_prompt()
    if base_prompt:
        system_prompt = f"{base_prompt}\n\n{system_prompt}"

    extra_tools = list(load_mcp_tools())  # contrato: [] em QUALQUER falha
    agent = create_deep_agent(
        model=_model_for(req.model),
        backend=fs,
        middleware=middleware,
        system_prompt=system_prompt,
        **({"tools": extra_tools} if extra_tools else {}),
    )
    return agent, usage_cb


MODEL_TEMPERATURE = 0.2
# `chat_template_kwargs` é o canal do vLLM/llama.cpp para ligar o modo de
# raciocínio dos modelos que trazem dois templates (Qwen3 e afins).
THINKING_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": True}}


def _model_for(model: str | None):
    """Instância de chat model com temperature baixa e thinking ligado.

    `create_deep_agent` aceita string OU BaseChatModel; a string usa o default
    do provider (temperature 1, thinking off nos templates duplos). Nem todo
    provider aceita `extra_body`, então qualquer falha na construção volta para
    a string crua — o comportamento de antes."""
    if not model:
        return model
    try:
        from langchain.chat_models import init_chat_model

        return init_chat_model(
            model,
            temperature=MODEL_TEMPERATURE,
            extra_body=THINKING_EXTRA_BODY,
        )
    except Exception:
        try:
            from langchain.chat_models import init_chat_model

            return init_chat_model(model, temperature=MODEL_TEMPERATURE)
        except Exception:
            return model


def _record_skill_usage(req: ExecRequest, skills: list[Any]) -> None:
    """Atribuição: marca as skills injetadas neste request no ledger.

    O id disponível é o melhor que existe: `run_id` se o request um dia ganhar
    o campo, senão `session_id` (limitação documentada em attribution.py).
    Guardado por try/except — atribuição nunca derruba o backend."""
    usage_id = getattr(req, "run_id", None) or req.session_id
    if not skills or not usage_id:
        return
    try:
        from harness.skills import attribution

        attribution.record_usage(usage_id, [s.name for s in skills])
    except Exception:
        pass


def _episodic_block(kind: str | None, prompt: str) -> str:
    """Memória episódica: falhas passadas do mesmo kind, ou "" se não há nada.

    Guardado por try/except e por import lazy — o módulo pode não existir num
    genoma antigo, e sqlite sem FTS5 devolve lista vazia."""
    try:
        from harness.memory import episodic

        return episodic.render_prompt(episodic.recall(kind, prompt))
    except Exception:
        return ""


def _executor_prompt() -> str:
    """Conteúdo de prompts/executor.md, ou "" se ausente/ilegível."""
    try:
        if EXECUTOR_PROMPT_PATH.is_file():
            return EXECUTOR_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _fs_allowlist(tools: tuple[str, ...]) -> list[str]:
    """Interseção do pedido com as tools de filesystem. Vazia => sem middleware
    custom (allowlist só de tools não-fs não deve derrubar o agente inteiro)."""
    if not tools:
        return []
    from typing import get_args

    from deepagents.middleware.filesystem import FsToolName

    known = list(get_args(FsToolName))
    return [t for t in known if t in tools]


def _hit_call_limit(messages: list[Any]) -> bool:
    for m in reversed(messages):
        if getattr(m, "type", None) == "ai":
            content = getattr(m, "content", "")
            return isinstance(content, str) and content.startswith(LIMIT_MESSAGE_PREFIX)
    return False


def _final_text(messages: list[Any]) -> str:
    """Texto da última mensagem do agente. "" = ele não disse nada.

    O content pode vir como lista de blocos (thinking + text): só os blocos
    `text` contam. Mensagem com tool_calls não é desistência, mesmo sem texto.
    """
    for m in reversed(messages):
        if getattr(m, "type", None) != "ai":
            continue
        content = getattr(m, "content", "")
        if isinstance(content, list):
            content = "".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict) and "text" in b
            )
        if str(content).strip():
            return str(content)
        return "" if not getattr(m, "tool_calls", None) else "[tool_calls]"
    return ""


def _turns(messages: list[Any]) -> int:
    return sum(1 for m in messages if getattr(m, "type", None) == "ai")


class _TraceMsg:
    """Mensagem mínima no formato que `_write_trace` lê (via getattr)."""

    def __init__(self, type: str, content: str, tool_calls: list[Any] | None = None) -> None:
        self.type = type
        self.content = content
        self.tool_calls = tool_calls


def _trace_collector() -> Any:
    """Callback que acumula as mensagens ENQUANTO o grafo roda.

    Timeout e GraphRecursionError não devolvem state: sem isto o trace desses
    runs sai vazio, e são justamente os que precisam de diagnóstico. Todo o
    corpo é best-effort — o coletor não pode derrubar o run que observa.
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class _Collector(BaseCallbackHandler):
        def __init__(self) -> None:
            self.messages: list[Any] = []

        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            try:
                for gen in getattr(response, "generations", []) or []:
                    for g in gen:
                        msg = getattr(g, "message", None)
                        if msg is not None:
                            self.messages.append(msg)
            except Exception:
                pass

        def on_tool_end(self, output: Any, **kwargs: Any) -> None:
            try:
                self.messages.append(_TraceMsg("tool", str(output)[:4000]))
            except Exception:
                pass

        def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
            try:
                self.messages.append(_TraceMsg("tool", str(error)[:4000]))
            except Exception:
                pass

    return _Collector()


def _with_timeout(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Roda `fn` numa thread daemon e desiste depois de `timeout_s`.

    Thread daemon em vez de `ThreadPoolExecutor`: o executor registra um
    `atexit` que espera a thread, o que faria o processo travar no fim justamente
    no caso de timeout (o invoke não é interrompível).
    """
    box: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            box.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 — repassado na thread chamadora
            box.put((False, exc))

    threading.Thread(target=target, daemon=True).start()
    try:
        ok, value = box.get(timeout=timeout_s)
    except queue.Empty:
        raise TimeoutError(f"invoke passou de {timeout_s}s") from None
    if not ok:
        raise value
    return value


# --------------------------------------------------------------------------- resultado


def _snapshot(root: Path, exclude: Path | None = None) -> dict[str, tuple[int, int]]:
    """Assinatura (mtime_ns, size) de cada arquivo do workspace, path relativo."""
    if not root.is_dir():
        return {}
    skip = exclude.resolve() if exclude else None
    out: dict[str, tuple[int, int]] = {}
    for p in root.rglob("*"):
        if not p.is_file() or (skip and p.resolve() == skip):
            continue
        st = p.stat()
        out[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return out


def _diff(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> tuple[str, ...]:
    keys = set(before) | set(after)
    return tuple(sorted(k for k in keys if before.get(k) != after.get(k)))


def load_pricing(config_dir: Path | None = None) -> dict[str, dict[str, float]]:
    path = (config_dir or Path(os.environ.get(CONFIG_DIR_ENV, "config"))) / MODELS_FILE
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("pricing", {})


def cost_usd(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    pricing: dict[str, dict[str, float]] | None = None,
) -> float | None:
    """None = não sabemos o preço. Nunca chutar zero para modelo pago."""
    table = load_pricing() if pricing is None else pricing
    entry = table.get(model or "")
    if entry is None:
        return 0.0 if model and model.startswith(OLLAMA_PREFIX) else None
    return (
        tokens_in * float(entry.get("input_per_mtok", 0.0))
        + tokens_out * float(entry.get("output_per_mtok", 0.0))
    ) / 1_000_000


def _tokens(usage: Any) -> tuple[int, int]:
    data = getattr(usage, "usage_metadata", None) or {}
    tin = sum(int(v.get("input_tokens", 0) or 0) for v in data.values())
    tout = sum(int(v.get("output_tokens", 0) or 0) for v in data.values())
    return tin, tout


def _result(
    req: ExecRequest,
    before: dict[str, tuple[int, int]],
    *,
    ok: bool,
    exit_reason: ExitReason,
    turns: int,
    usage: Any,
    after: dict[str, tuple[int, int]] | None = None,
) -> ExecResult:
    tin, tout = _tokens(usage)
    return ExecResult(
        ok=ok,
        exit_reason=exit_reason,
        turns=turns,
        cost_usd=cost_usd(req.model, tin, tout),
        tokens_in=tin,
        tokens_out=tout,
        # `after` reaproveita o snapshot de quem já decidiu por ele (o "stalled"
        # precisa do diff antes de escolher o exit_reason); sem ele, tira agora.
        files_changed=_diff(
            before,
            after if after is not None else _snapshot(req.workspace, exclude=req.trace_path),
        ),
        session_id=req.session_id,
        trace_path=req.trace_path,
    )


def _failure(
    req: ExecRequest, exit_reason: ExitReason, reason: str, messages: Sequence[Any] = ()
) -> ExecResult:
    _write_trace(req.trace_path, list(messages), error=reason)
    return ExecResult(
        ok=False,
        exit_reason=exit_reason,
        turns=0,
        cost_usd=None,
        tokens_in=None,
        tokens_out=None,
        files_changed=(),
        session_id=req.session_id,
        trace_path=req.trace_path,
    )


def _write_trace(path: Path, messages: list[Any], error: str | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            if error:
                fh.write(json.dumps({"error": error}, ensure_ascii=False) + "\n")
            for m in messages:
                rec = {
                    "type": getattr(m, "type", "?"),
                    "content": str(getattr(m, "content", ""))[:4000],
                }
                calls = getattr(m, "tool_calls", None)
                if calls:
                    rec["tool_calls"] = [c.get("name") for c in calls]
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:  # trace é diagnóstico, não pode derrubar o run
        pass
