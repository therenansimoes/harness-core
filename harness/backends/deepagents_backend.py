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

from harness.backends.agent_roles import load_roles, roles_manual
from harness.skills import render_prompt, select_skills
from harness.types import Capabilities, ExecRequest, ExecResult, ExitReason, Preflight

try:
    from harness.backends.mcp_tools import load_mcp_tools
except Exception:  # módulo chega em PR paralelo; sem ele, sem tools extras
    load_mcp_tools = lambda *a, **k: []  # noqa: E731

# LM Studio é o ÚNICO runtime local (Ollama cortado em 2026-08-04): MLX na porta
# 1234 atrás de um endpoint OpenAI-compatível, por isso o prefixo `openai:`.
OPENAI_PREFIX = "openai:"
LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
LMSTUDIO_BASE_URL_ENV = "OPENAI_BASE_URL"
LMSTUDIO_KEY_ENV = "OPENAI_API_KEY"
LMSTUDIO_TIMEOUT_S = 3.0

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
# Manual das tools (o que cada uma faz, assinatura, exemplo e pegadinha). Cada
# modelo usa tool de um jeito; prompts/tools/<provider>_<modelo>.md e
# prompts/tools/<provider>.md ganham do geral quando existem.
TOOLS_PROMPT_PATH = Path("prompts/tools.md")
TOOLS_PROMPT_DIR = Path("prompts/tools")


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
        if model and model.startswith(OPENAI_PREFIX):
            return _lmstudio_preflight(model)
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
        # `todos` é estado do TodoListMiddleware, não mensagem: sem esta linha o
        # plano que o agente seguiu não sobra em nenhum lugar depois do run.
        _write_trace(req.trace_path, messages, todos=state.get("todos"))
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
    # `openai:*` aqui é LM Studio, não a nuvem: sem estes defaults o
    # langchain-openai aponta para api.openai.com e o run morre em 401 DEPOIS do
    # preflight passar (o preflight sonda loopback). Os dois têm que concordar
    # por construção. `setdefault`: env explícito continua ganhando, e é assim
    # que se aponta para a nuvem de propósito (OPENAI_BASE_URL + chave real).
    os.environ.setdefault(LMSTUDIO_BASE_URL_ENV, LMSTUDIO_BASE_URL)
    # O LM Studio ignora o valor, mas o cliente exige que exista.
    os.environ.setdefault(LMSTUDIO_KEY_ENV, "lm-studio")


def _import_deepagents():
    import deepagents

    return deepagents


# --------------------------------------------------------------------------- LM Studio


def _lmstudio_base_url() -> str:
    return (os.environ.get(LMSTUDIO_BASE_URL_ENV) or LMSTUDIO_BASE_URL).rstrip("/")


def _lmstudio_models(url: str) -> set[str]:
    """Ids servidos agora. Levanta OSError/ValueError se o servidor não fala."""
    req = urllib.request.Request(url)
    # Endpoint compatível atrás de auth (LM Studio ignora, cloud exige).
    key = os.environ.get(LMSTUDIO_KEY_ENV, "")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=LMSTUDIO_TIMEOUT_S) as resp:
        payload = json.loads(resp.read())
    return {str(m.get("id", "")) for m in payload.get("data", [])}


def _lmstudio_preflight(model: str) -> Preflight:
    """Servidor vivo E modelo servido. Sonda `GET /v1/models`, zero token gasto."""
    wanted = model[len(OPENAI_PREFIX) :]
    url = f"{_lmstudio_base_url()}/models"
    try:
        served = _lmstudio_models(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Preflight(
            ok=False,
            reason=(
                f"LM Studio não respondeu em {url} após {LMSTUDIO_TIMEOUT_S}s ({exc}) — "
                "abra o app e rode `lms server start`"
            ),
        )
    if wanted not in served:
        listed = ", ".join(sorted(served)) or "nenhum"
        return Preflight(
            ok=False,
            reason=(
                f"modelo {wanted!r} não está baixado/servido pelo LM Studio "
                f"(tem: {listed}) — `lms load {wanted}`"
            ),
        )
    return Preflight(ok=True, reason=f"LM Studio ok em {url}, modelo {wanted} servido")


# --------------------------------------------------------------------------- agente

# Bloco do TodoListMiddleware. Curto de propósito: o default da lib é longo e em
# inglês, e o que o executor precisa saber cabe em cinco linhas.
TODO_PROMPT = (
    "## `write_todos`\n\n"
    "Tarefa que toca mais de um arquivo, ou que pede refactor/implementar: "
    "chame `write_todos` com o plano ANTES de editar qualquer coisa.\n"
    "No máximo 7 itens, cada um com o path do arquivo envolvido.\n"
    "Exatamente UM item em `in_progress` por vez; marque `completed` na hora que "
    "terminar o item, nunca em lote no fim.\n"
    "A chamada substitui a lista inteira: reenvie todos os itens, com o status "
    "atualizado de cada um.\n"
    "Tarefa de um passo só não precisa de lista — faça e reporte."
)


def _build_agent(req: ExecRequest):
    from deepagents import create_deep_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware, TodoListMiddleware
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
    # O shell entra pela SafeShellBackend: `virtual_mode` NÃO cobre `execute`
    # (a doc do deepagents é explícita), então a cerca (denylist + workspace +
    # timeout) é o que impede o loop autônomo de mexer na máquina.
    from harness.backends.safe_shell import SafeShellBackend

    fs = SafeShellBackend(root_dir=str(req.workspace), virtual_mode=True)

    middleware: list[Any] = []
    allowed = _fs_allowlist(req.tools)
    if allowed:
        # `tools=` do create_deep_agent é aditivo; a única forma de restringir é
        # substituir o FilesystemMiddleware (merge por `.name`).
        from harness.backends.smart_fs import SmartFilesystemMiddleware

        middleware.append(SmartFilesystemMiddleware(backend=fs, tools=allowed))
    middleware.append(ModelCallLimitMiddleware(run_limit=req.max_turns, exit_behavior="end"))
    # Planejamento como ESTADO, não como parágrafo perdido no meio da conversa:
    # a lista vive em `state["todos"]` e o middleware reinjeta ela a cada turno.
    # `system_prompt=` substitui o bloco default (inglês, ~40 linhas) — em pt-br
    # e curto porque o executor é modelo pequeno e o contexto é disputado.
    middleware.append(TodoListMiddleware(system_prompt=TODO_PROMPT))

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
    # `query=` faz o ranking por relevância à unidade e o teto corta o resto:
    # mandar todas as skills do kind enchia o contexto do executor pequeno com
    # guidance que não tem nada a ver com a tarefa.
    skills = select_skills(getattr(req, "kind", None), query=req.prompt)
    skills_block = render_prompt(skills)
    if skills_block:
        system_prompt = f"{system_prompt}\n\n{skills_block}"
    _record_skill_usage(req, skills)

    recall_block = _episodic_block(getattr(req, "kind", None), req.prompt)
    if recall_block:
        system_prompt = f"{system_prompt}\n\n{recall_block}"

    # Manual das tools depois das pontes: modelo pequeno não descobre sozinho
    # que tool é o único caminho para virar arquivo, nem as pegadinhas de cada
    # uma. Sem isso o run termina "explicando" a mudança e nada é escrito.
    tools_block = _tools_prompt(req.model)
    if tools_block:
        system_prompt = f"{system_prompt}\n\n{tools_block}"

    base_prompt = _executor_prompt()
    if base_prompt:
        system_prompt = f"{base_prompt}\n\n{system_prompt}"

    # Papéis vêm de config/agents.toml (dado, não código): [] devolve o
    # comportamento de antes — só o `general-purpose` default da tool `task`.
    roles = load_roles(backend=fs, allowed=allowed)
    manual = roles_manual(roles)
    if manual:
        system_prompt = f"{system_prompt}\n\n{manual}"

    extra_tools = list(load_mcp_tools())  # contrato: [] em QUALQUER falha
    # Tools de engenharia (edição cirúrgica) e de web. Cada import é fail-open
    # no mesmo padrão do resto do arquivo: dependência ausente/quebrada vira
    # lista vazia, o run segue com o que tem.
    try:
        from harness.backends.file_tools import make_file_tools

        extra_tools += list(make_file_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.backends.web_tools import load_web_tools

        extra_tools += list(load_web_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.backends.flow_tools import load_flow_tools

        extra_tools += list(load_flow_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.backends.procs import make_proc_tools

        extra_tools += list(make_proc_tools(req.workspace))
    except Exception:
        pass
    agent = create_deep_agent(
        model=_model_for(req.model),
        backend=fs,
        middleware=middleware,
        system_prompt=system_prompt,
        **({"tools": extra_tools} if extra_tools else {}),
        **({"subagents": roles} if roles else {}),
    )
    return agent, usage_cb


MODEL_TEMPERATURE = 0.2
# `chat_template_kwargs` é o canal do vLLM/llama.cpp para ligar o modo de
# raciocínio dos modelos que trazem dois templates (Qwen3 e afins).
THINKING_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": True}}


def _thinking_kwargs(model: str) -> dict[str, Any]:
    """Canal de thinking do provider — medido, não suposto (2026-08).

    (O ramo `ollama:*`/`reasoning=True` saiu com o runtime, cortado 2026-08-04.)

    `openai:*` (LM Studio) — não há nada para pedir. Sondagem contra o servidor
    vivo (qwen3.5-9b-mlx em /v1) com e sem `chat_template_kwargs.enable_thinking`
    volta `reasoning_content` preenchido nas duas: o thinking já é o default do
    servidor e a flag não muda o comportamento. Mandamos ela mesmo assim porque
    vLLM/llama.cpp atrás de um endpoint openai-compatível só ligam por aí, e o
    LM Studio a ignora sem erro. Limite conhecido, não contornável daqui:
    langchain-openai 1.4.1 declara na própria docstring que NÃO extrai
    `reasoning_content` de endpoints compatíveis — o modelo raciocina, mas o
    trace não aparece no `AIMessage`.

    `anthropic:*` e o resto — default do provider, como antes."""
    if model.startswith(OPENAI_PREFIX):
        return {"extra_body": THINKING_EXTRA_BODY}
    return {}


def _model_for(model: str | None):
    """Instância de chat model com temperature baixa e thinking ligado.

    `create_deep_agent` aceita string OU BaseChatModel; a string usa o default
    do provider (temperature 1, thinking off nos templates duplos). O canal de
    thinking varia por provider (ver `_thinking_kwargs`), e provider que rejeita
    o kwarg cai para só-temperature e depois para a string crua — o
    comportamento de antes."""
    if not model:
        return model
    from_provider = _thinking_kwargs(model)
    for kwargs in (from_provider, {}) if from_provider else ({},):
        try:
            from langchain.chat_models import init_chat_model

            return init_chat_model(model, temperature=MODEL_TEMPERATURE, **kwargs)
        except Exception:
            continue
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


def _tools_prompt(model: str | None) -> str:
    """Manual das tools para este modelo, ou "" se nenhum arquivo existir.

    Variação por modelo com fallback: "openai:qwen3.5-9b-mlx" tenta
    prompts/tools/openai_qwen3.5-9b-mlx.md, prompts/tools/openai.md e por fim
    prompts/tools.md. Fail-open igual ao executor.md — genoma sem o arquivo
    volta ao comportamento de antes."""
    for path in _tools_prompt_candidates(model):
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def _tools_prompt_candidates(model: str | None) -> list[Path]:
    candidates: list[Path] = []
    if model:
        provider, _, name = model.partition(":")
        slug = _slug(f"{provider}_{name}") if name else ""
        if slug:
            candidates.append(TOOLS_PROMPT_DIR / f"{slug}.md")
        if provider:
            candidates.append(TOOLS_PROMPT_DIR / f"{_slug(provider)}.md")
    candidates.append(TOOLS_PROMPT_PATH)
    return candidates


def _slug(raw: str) -> str:
    """Nome de modelo em nome de arquivo (o "/" de "openai:org/modelo" e o ":"
    de "qwen3.5-9b-mlx" não podem virar diretório nem escapar de prompts/)."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in raw).strip("_")


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
        # Fora da tabela = sem preço conhecido, ponto. Os locais do LM Studio já
        # estão em [pricing] a 0.0 (o atalho `ollama:*`-vale-0 saiu com o runtime cortado).
        return None
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


def _write_trace(
    path: Path, messages: list[Any], error: str | None = None, todos: Any = None
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            if error:
                fh.write(json.dumps({"error": error}, ensure_ascii=False) + "\n")
            if todos:
                # Linha própria, mesmo formato JSONL: `type` continua sendo a
                # chave que distingue os registros para quem lê o trace.
                fh.write(
                    json.dumps({"type": "todos", "todos": _todo_records(todos)}, ensure_ascii=False)
                    + "\n"
                )
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


def _todo_records(todos: Any) -> list[dict[str, str]]:
    """`state["todos"]` em linha de trace: só `content` e `status`, texto cortado.

    Item é um dict (TodoItem do middleware), mas o estado vem do checkpointer e
    pode ter vindo de versão antiga — `getattr`/`get` defensivo, sem levantar."""
    out: list[dict[str, str]] = []
    for t in todos or ():
        if isinstance(t, dict):
            content, status = t.get("content", ""), t.get("status", "")
        else:
            content, status = getattr(t, "content", ""), getattr(t, "status", "")
        out.append({"content": str(content)[:500], "status": str(status)})
    return out
