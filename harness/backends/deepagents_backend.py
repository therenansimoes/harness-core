"""Backend deepagents: o único arquivo do repo que conhece LangChain.

Risco 1 da SPEC — a API do deepagents é jovem. Todo import de
`deepagents`/`langchain`/`langgraph` mora aqui e é *lazy* (dentro do método),
para que `harness backends` funcione com o extra desinstalado.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, ClassVar

from harness import trust_boundary
from harness.backends.agent_roles import load_roles, roles_manual
from harness.skills import render_prompt, select_skills
from harness.types import Capabilities, ExecRequest, ExecResult, ExitReason, Preflight

try:
    from harness.backends.mcp_tools import load_mcp_tools
except Exception:  # módulo chega em PR paralelo; sem ele, sem tools extras
    load_mcp_tools = lambda *a, **k: []  # noqa: E731

# Runtime local default: LM Studio na porta 1234 (OpenAI-compat + GPU offload).
# Prefixo `openai:` = endpoint OpenAI-shaped em loopback, NÃO a nuvem.
OPENAI_PREFIX = "openai:"
LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"  # LM Studio (offload)
LMSTUDIO_BASE_URL_ENV = "OPENAI_BASE_URL"
LMSTUDIO_KEY_ENV = "OPENAI_API_KEY"
LMSTUDIO_TIMEOUT_S = 3.0

# Mesmo host default que OPENAI_BASE_URL — frota LoRA e t0 compartilham :1235.
# Override separado: HARNESS_MLX_BASE_URL (adapters runtime=mlx).
MLX_BASE_URL = "http://127.0.0.1:1235/v1"
MLX_BASE_URL_ENV = "HARNESS_MLX_BASE_URL"
RUNTIME_MLX = "mlx"
DEFAULT_LOCAL_MODEL = "qwopus3.5-4b-coder-mtp"

CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"
MODELS_FILE = "models.toml"

# Sentinela do `ModelCallLimitMiddleware` (langchain 1.3.14,
# `_build_limit_exceeded_message`): é como o limite de turnos se anuncia, já que
# nada no retorno do grafo diz por que ele parou.
LIMIT_MESSAGE_PREFIX = "Model call limits exceeded:"

# Gatilho da compactação, calibrado pro executor local (9B MLX, ctx de 32k):
# ~60% do contexto, o que deixa folga pro system prompt (executor + skills +
# manual das tools + recall, já na casa dos milhares) e pra resposta do turno.
# Acima disso o provider começa a cortar a saída em vez de errar.
CONTEXT_WINDOW_TOKENS = 32_768
CONTEXT_TRIGGER_TOKENS = int(CONTEXT_WINDOW_TOKENS * 0.6)
# Quantos tool results recentes ficam intactos. 2 = o do turno anterior e o de
# antes dele — o suficiente pro modelo continuar de onde parou.
CONTEXT_KEEP_TOOL_RESULTS = 2

# finish_reason que o provider usa pra dizer "cortei no teto de tokens".
# `length` é o do OpenAI/LM Studio; `max_tokens` é o do Anthropic (stop_reason).
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

_INSTALL_HINT = "deepagents não instalado — pip install harness-core[deepagents]"

# Chat models instanciados para o request em curso, para o `_abort_http` alcançar
# o cliente HTTP no timeout. ContextVar e não global: dois runs podem estar em
# threads diferentes do mesmo processo, e fechar a conexão do vizinho seria pior
# que o vazamento que isto conserta. `None` = ninguém está coletando (chamada de
# `_model_for` fora de um `execute`), e aí nada é registrado.
_HTTP_CLIENTS: ContextVar[list[Any] | None] = ContextVar("harness_http_clients", default=None)

# Contrato com o prompt-builder: se este arquivo existir, seu conteúdo é
# prependado às instructions do agente; ausente => comportamento atual.
EXECUTOR_PROMPT_PATH = Path("prompts/executor.md")
# Manual das tools (o que cada uma faz, assinatura, exemplo e pegadinha). Cada
# modelo usa tool de um jeito; prompts/tools/<provider>_<modelo>.md e
# prompts/tools/<provider>.md ganham do geral quando existem.
TOOLS_PROMPT_PATH = Path("prompts/tools.md")
TOOLS_PROMPT_DIR = Path("prompts/tools")

# Constituição do REPO-ALVO (o workspace do run), em ordem de prioridade: o
# `-exec` existe para o projeto dizer o que vale para um agente executor sem
# misturar com o AGENTS.md que ele mantém para humanos/outras ferramentas.
TARGET_CONSTITUTION_FILES = ("AGENTS-exec.md", "AGENTS.md")
# Teto de caracteres: constituição é regra, mas system prompt de executor 9B é
# recurso disputado (ver CONTEXT_TRIGGER_TOKENS). Acima do teto entra o começo
# do arquivo com aviso de corte, para o modelo não tratar o fim como completo.
TARGET_CONSTITUTION_MAX_CHARS = 2000

# Alvos de arquivo citados no prompt da unidade — a fonte barata de `files=` para
# o path-trigger das skills: determinístico, sem plumbing novo e sem depender de
# o run já ter mexido em nada (o `files_changed` só existe DEPOIS de executar).
PROMPT_FILE_RE = re.compile(r"[\w./-]+\.(?:py|js|ts|html|css|toml|json|md)\b")


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

    def _preflight(self, model: str | None, adapter: Any = None) -> Preflight:
        try:
            _import_deepagents()
        except ImportError as exc:
            return Preflight(ok=False, reason=f"{_INSTALL_HINT} ({exc})")
        # Com adapter da frota quem atende é o servidor DELE: sondar o LM Studio
        # bloquearia um run que ia rodar no MLX só porque o app 1234 está fechado.
        if adapter is not None and adapter.runtime == RUNTIME_MLX:
            return _mlx_preflight(adapter)
        if model and model.startswith(OPENAI_PREFIX):
            return _lmstudio_preflight(model)
        return Preflight(ok=True, reason="deepagents importável")

    def execute(self, req: ExecRequest) -> ExecResult:
        _bootstrap_env()

        pre = self._preflight(req.model, _adapter_for(req.adapter))
        if not pre.ok:
            return _failure(req, "blocked", pre.reason)

        # Blocker de tentativa ANTERIOR não vale para esta: sidecar vivo aqui
        # viraria exit_reason="blocker" num run que nem chamou a tool.
        _clear_blocker(req.workspace)

        before = _snapshot(req.workspace, exclude=req.trace_path)
        # Handles de cliente HTTP deste request, para o abort do timeout (ver
        # `_abort_http`). Coletados por contextvar porque quem instancia o chat
        # model é o `_build_agent` lá embaixo e a assinatura dele é seam de
        # monkeypatch em meia dúzia de testes.
        clients: list[Any] = []
        token = _HTTP_CLIENTS.set(clients)
        try:
            agent, usage_cb = _build_agent(req)
        finally:
            _HTTP_CLIENTS.reset(token)
        tracer = _trace_collector()
        config: dict[str, Any] = {
            # `recursion_limit` conta passos do grafo (modelo + tools + middleware),
            # não turnos; folga de 4x para o limite real ser o middleware.
            "recursion_limit": max(50, req.max_turns * 4),
            "callbacks": [usage_cb, tracer],
        }

        payload = {"messages": _payload_messages(req)}
        try:
            state = _with_timeout(
                lambda: agent.invoke(payload, config),
                req.timeout_s,
                on_timeout=lambda: _abort_http(clients),
            )
        except TimeoutError:
            # Cópia: a thread do invoke pode levar ~1s para desenrolar depois do
            # abort e até lá segue mutando a lista.
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
        declared = _read_blocker(req.workspace)
        exit_reason: ExitReason
        if declared is not None:
            # Declarar vence stalled/done: o motivo dito pelo modelo é melhor
            # sinal que qualquer um inferido daqui, e é o que o gate roteia.
            return _result(
                req,
                before,
                ok=False,
                exit_reason="blocker",
                turns=turns,
                usage=usage_cb,
                after=after,
                blocker=declared[0],
            )
        if hit_limit:
            exit_reason, ok = "max_turns", False
            turns = max(0, turns - 1)  # a mensagem-sentinela não é um turno do agente
        elif _truncated(messages):
            # Resposta cortada no teto de tokens não é conclusão: o run pode ter
            # escrito algo (a régua ainda julga), mas o motivo da parada é o
            # provider, não o agente. "done" aqui vira braço ruim no bandit.
            exit_reason, ok = "truncated", False
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
    # `openai:*` aqui é mlx_lm.server (loopback), não a nuvem: sem estes
    # defaults o langchain-openai aponta para api.openai.com e o run morre em
    # 401 DEPOIS do preflight. `setdefault`: env explícito ganha.
    os.environ.setdefault(LMSTUDIO_BASE_URL_ENV, LMSTUDIO_BASE_URL)
    os.environ.setdefault(LMSTUDIO_KEY_ENV, "mlx-local")


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


def _model_listed(wanted: str, served: set[str]) -> bool:
    """mlx_lm.server costuma listar o path do peso ou `default`, não o alias
    estável `qwopus3.5-4b-coder-mtp` que o harness/Cursor usam."""
    if not served:
        return True
    if wanted in served or "default" in served:
        return True
    w = wanted.lower()
    return any(w in s.lower() or s.lower().rstrip("/").endswith(w) for s in served)


def _lmstudio_preflight(model: str) -> Preflight:
    """Servidor OpenAI-local vivo. Sonda `GET /v1/models`, zero token gasto.

    Nome legado `_lmstudio_*`; o default aponta pro mlx_lm.server (:1235)."""
    wanted = model[len(OPENAI_PREFIX) :]
    url = f"{_lmstudio_base_url()}/models"
    try:
        served = _lmstudio_models(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Preflight(
            ok=False,
            reason=(
                f"mlx_lm.server não respondeu em {url} após {LMSTUDIO_TIMEOUT_S}s ({exc}) — "
                "suba com `scripts/mlx_bonsai.sh` (ou defina OPENAI_BASE_URL)"
            ),
        )
    if not _model_listed(wanted, served):
        listed = ", ".join(sorted(served)) or "nenhum"
        return Preflight(
            ok=False,
            reason=(
                f"modelo {wanted!r} não aparece em {url} (tem: {listed}) — "
                "confira o --model do mlx_lm.server / OPENAI_BASE_URL"
            ),
        )
    return Preflight(ok=True, reason=f"mlx ok em {url}, modelo {wanted}")


# --------------------------------------------------------------------------- frota LoRA


def _mlx_base_url() -> str:
    return (os.environ.get(MLX_BASE_URL_ENV) or MLX_BASE_URL).rstrip("/")


def _adapter_for(adapter_id: str | None):
    """Resolve o id que o router carimbou. Fail-open no padrão do arquivo:
    registro ilegível não derruba um run já roteado — sem adapter, a base
    atende."""
    if not adapter_id:
        return None
    try:
        from harness.routing.adapters import get_adapter

        return get_adapter(adapter_id)
    except Exception:
        return None


def _mlx_preflight(adapter: Any) -> Preflight:
    """Servidor MLX vivo E servindo o base do adapter. Mesma sonda barata do LM
    Studio, outro processo: quem sobe é o Makefile, não o harness."""
    url = f"{_mlx_base_url()}/models"
    try:
        served = _lmstudio_models(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Preflight(
            ok=False,
            reason=(
                f"servidor MLX não respondeu em {url} após {LMSTUDIO_TIMEOUT_S}s ({exc}) — "
                f"suba o mlx_lm.server com o base {adapter.served_model}"
            ),
        )
    # Servidor que não lista nada ainda serve o base carregado na linha de
    # comando: lista vazia não é prova de ausência, e bloquear aí seria falso
    # negativo. Só o nome CONTRADITÓRIO bloqueia.
    if served and adapter.served_model not in served:
        listed = ", ".join(sorted(served))
        return Preflight(
            ok=False,
            reason=(
                f"servidor MLX em {url} não serve o base {adapter.served_model!r} do adapter "
                f"{adapter.id!r} (tem: {listed})"
            ),
        )
    return Preflight(ok=True, reason=f"MLX ok em {url}, adapter {adapter.id} pronto")


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

# Ordem da frota colapsada (`collapse_fleet`): o gasto já passou do valor da
# tarefa, delegar é mais uma rodada de tokens que ninguém vai pagar.
NO_FLEET_ORDER = (
    "Neste run você NÃO delega: não chame a tool `task`. "
    "O orçamento desta tarefa já estourou — resolva você mesmo, no menor "
    "número de passos possível."
)


def _fleet(roles: list[dict], req: ExecRequest) -> tuple[list[dict], str]:
    """(frota deste run, ordem para o system prompt) depois do reorg.

    A frota do `agents.toml` é o default; o reorg só a RECORTA. `roles_allow`
    ausente (`None`, o normal) não filtra nada e o run é o de sempre; `()` é a
    frota colapsada — nenhum subagent chega ao `create_deep_agent` e o prompt
    ganha a ordem de não delegar. `roles_required` é pedido por NOME: papel que
    não existe no toml não vira ordem nenhuma, porque o modelo chamaria um
    `subagent_type` inexistente e queimaria o turno no erro da tool.

    Fail-open igual ao resto do arquivo: qualquer coisa torta aqui devolve a
    frota que entrou e nenhuma ordem — reorg não derruba execução."""
    try:
        allow = getattr(req, "roles_allow", None)
        if allow is not None:
            nomes = {n for n in allow if isinstance(n, str)}
            roles = [r for r in roles if r.get("name") in nomes]
            if not roles:
                return [], NO_FLEET_ORDER
        vivos = {r.get("name") for r in roles}
        pedidos = [n for n in (getattr(req, "roles_required", ()) or ()) if n in vivos]
        if not pedidos:
            return roles, ""
        ordens = "\n".join(
            f"- antes de terminar, chame task(subagent_type={n!r}) e corrija o que ele apontar."
            for n in pedidos
        )
        return roles, f"Este run tem papel OBRIGATÓRIO na frota:\n{ordens}"
    except Exception:
        return roles, ""


def _shell_backend(workspace: Path):
    """SafeShellBackend, ou a variante com sandbox de SO quando [executor]
    sandbox != "off" no tools.toml. Fail-open no setup: sandbox indisponível
    => shell de sempre."""
    from harness.backends.safe_shell import SafeShellBackend

    try:
        from harness.backends import sandbox as _sandbox

        sbx = _sandbox.make_sandbox(workspace, _sandbox.load_settings())
        if sbx is not None:
            from harness.backends.sandbox_shell import SandboxedShellBackend

            return SandboxedShellBackend(root_dir=str(workspace), virtual_mode=True, sandbox=sbx)
    except Exception:
        pass  # fail-open: sandbox nunca derruba a construção do agente
    return SafeShellBackend(root_dir=str(workspace), virtual_mode=True)


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
    # Com [executor] sandbox='workspace-write' no tools.toml, o `execute` ainda
    # passa pela mesma cerca e ADICIONALMENTE roda sob Seatbelt (escrita só no
    # workspace+temp).
    fs = _shell_backend(req.workspace)

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
    # Compactação DETERMINÍSTICA: tool result velho vira placeholder quando a
    # conversa passa do gatilho. Sem isto o run longo do 9B local estoura o ctx
    # e o provider corta a resposta no meio (o `truncated` do execute). Não é
    # summarization — nenhuma chamada de LLM extra, o que importa quando o único
    # modelo disponível é o mesmo que está executando a tarefa.
    # Fail-open no padrão do arquivo: middleware ausente na versão instalada da
    # lib => o run segue sem compactação, como antes.
    try:
        from langchain.agents.middleware import (
            ClearToolUsesEdit,
            ContextEditingMiddleware,
        )

        middleware.append(
            ContextEditingMiddleware(
                edits=[
                    ClearToolUsesEdit(
                        trigger=CONTEXT_TRIGGER_TOKENS,
                        # 0 (default) = limpa TODOS os elegíveis numa passada.
                        # Medido: com `clear_at_least>0` o edit para na primeira
                        # limpeza que atinge a cota e o contexto continua ACIMA
                        # do gatilho — o turno seguinte é cortado do mesmo jeito.
                        # Tool result velho não é perdido de verdade: o agente
                        # relê o arquivo com read_file se precisar.
                        clear_at_least=0,
                        keep=CONTEXT_KEEP_TOOL_RESULTS,
                        # Limpar o INPUT da tool também apaga o path que o modelo
                        # pediu; modelo pequeno relê o mesmo arquivo em loop sem
                        # esse rastro. Só o output sai.
                        clear_tool_inputs=False,
                        # `write_file`/`edit_file` devolvem a confirmação do que
                        # foi escrito — é o registro de que a tarefa andou.
                        exclude_tools=("write_file", "edit_file"),
                    )
                ],
                # "model" chamaria `get_num_tokens_from_messages` a cada turno;
                # no LM Studio isso é ida na rede (ou tiktoken errado pro modelo
                # local). A aproximação erra pra cima e o gatilho já tem folga.
                token_count_method="approximate",
            )
        )
    except Exception:
        pass
    # ANTES do retry de propósito: primeiro = mais externo, então a guarda conta
    # as calls que o MODELO fez e as tentativas de infra ficam invisíveis para
    # ela. Invertido, erro de rede repetido sairia como "você está em loop".
    try:
        from harness.backends.loop_guard import LoopGuardMiddleware

        middleware.append(LoopGuardMiddleware())
    except Exception:
        pass
    # Erro de tool transitório (rede da web_tools, lock de arquivo, subprocess
    # que morreu) gastava um turno do agente e virava texto de erro no contexto.
    try:
        from langchain.agents.middleware import ToolRetryMiddleware

        middleware.append(
            ToolRetryMiddleware(
                max_retries=2,
                # 0.5s → 1s: o run tem timeout de parede, então backoff longo
                # (default 1s * 2^n com teto de 60s) come o prazo da tarefa.
                initial_delay=0.5,
                backoff_factor=2.0,
                max_delay=4.0,
                # Falha depois das tentativas volta como ToolMessage: o modelo
                # decide o que fazer. "error" mataria o run inteiro.
                on_failure="continue",
            )
        )
    except Exception:
        pass

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
    # `files=` liga o path-trigger: skill com glob (`paths = ["*.toml"]`) fura a
    # fila do ranking fuzzy quando a unidade nomeia o arquivo que vai mexer.
    skills = _selected_skills(req)
    _record_skill_usage(req, skills)
    recall_block = _episodic_block(getattr(req, "kind", None), req.prompt)
    if trust_boundary.enabled():
        # Corpo de skill e trace de falha antiga são texto que o loop escreveu
        # sozinho: no system prompt eles ganham a NOSSA autoridade. Aqui sobra o
        # índice (nome — descrição) mais o aviso de fronteira; os corpos vão no
        # bloco não confiável da mensagem do usuário (ver `_untrusted_block`).
        index = _skills_index(skills)
        if index:
            system_prompt = f"{system_prompt}\n\n{index}"
        if index or recall_block:
            system_prompt = f"{system_prompt}\n\n{trust_boundary.BOUNDARY_NOTE}"
    else:
        skills_block = render_prompt(skills)
        if skills_block:
            system_prompt = f"{system_prompt}\n\n{skills_block}"
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
    # `model` só serve ao papel que delega (o subagent aninhado é compilado na
    # hora): é a MESMA instância do agente principal, nenhum peso extra na
    # máquina.
    adapter = _adapter_for(req.adapter)
    chat_model = _model_for(req.model, adapter)
    # `_fleet` é o recorte que o reorg decidiu no route: frota inteira (o
    # normal), frota filtrada ou frota nenhuma. Com `roles == []` o manual já sai
    # "" e `subagents` nem chega ao create_deep_agent — o colapso é real, não uma
    # frase no prompt.
    roles, fleet_order = _fleet(load_roles(backend=fs, allowed=allowed, model=chat_model), req)
    manual = roles_manual(roles)
    if manual:
        system_prompt = f"{system_prompt}\n\n{manual}"
    if fleet_order:
        system_prompt = f"{system_prompt}\n\n{fleet_order}"

    # Por último de propósito: é a regra que vence as outras quando conflita, e
    # o fim do system prompt é o pedaço que o modelo pequeno mais respeita.
    constitution = _target_constitution(req.workspace)
    if constitution:
        system_prompt = f"{system_prompt}\n\n{constitution}"

    # O `system` do card do adapter (`config/adapters.toml`) é o texto com que
    # AQUELE peso foi treinado — taxonomia do juiz, regra do condensador. Vai na
    # FRENTE de tudo, inclusive do prompt do executor, porque é a posição em que
    # o LoRA o viu no fine-tuning; enfiado no meio ele vira mais um parágrafo.
    # Sem adapter (ou com `system` vazio, que é o default do registro) nada aqui
    # muda: o prompt é o mesmo de antes, byte a byte.
    if adapter is not None and (adapter.system or "").strip():
        system_prompt = f"{adapter.system.strip()}\n\n{system_prompt}"

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
    try:
        from harness.scaffold import make_scaffold_tools

        extra_tools += list(make_scaffold_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.backends.dom_tools import make_dom_tools, make_view_tools

        extra_tools += list(make_view_tools(req.workspace))
        extra_tools += list(make_dom_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.backends.review_tools import make_review_tools

        extra_tools += list(make_review_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.symbols import make_symbol_tools

        extra_tools += list(make_symbol_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.repomap import make_repomap_tools

        extra_tools += list(make_repomap_tools(req.workspace))
    except Exception:
        pass
    try:
        from harness.backends.blocker_tools import make_blocker_tools

        extra_tools += list(make_blocker_tools(req.workspace))
    except Exception:
        pass
    agent = create_deep_agent(
        model=chat_model,
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
THINKING_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}
# False desde 2026-08-06: o build qwen3.5-9b-mlx IGNORAVA a flag (sondagem de
# agosto) e escrevia; os builds novos (qwen/qwen3.5-9b, qwopus-coder) OBEDECEM —
# com True o turno afoga em reasoning_content e o agente lê mas nunca escreve
# (runs 7f71cce2457b, 1d9542428a9c e smoke-coder2: zero write_file em todos).


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


def _model_for(model: str | None, adapter: Any = None):
    """Instância de chat model com temperature baixa e thinking ligado.

    `create_deep_agent` aceita string OU BaseChatModel; a string usa o default
    do provider (temperature 1, thinking off nos templates duplos). O canal de
    thinking varia por provider (ver `_thinking_kwargs`), e provider que rejeita
    o kwarg cai para só-temperature e depois para a string crua — o
    comportamento de antes.

    Com adapter da frota o caminho é outro (`_adapter_model`); sem adapter,
    nada abaixo muda."""
    if adapter is not None:
        return _adapter_model(model, adapter)
    if not model:
        return model
    from_provider = _thinking_kwargs(model)
    for kwargs in (from_provider, {}) if from_provider else ({},):
        try:
            from langchain.chat_models import init_chat_model

            return _track(init_chat_model(model, temperature=MODEL_TEMPERATURE, **kwargs))
        except Exception:
            continue
    return model


def _track(model: Any) -> Any:
    """Anota o chat model na coleta do request em curso. Devolve o próprio."""
    sink = _HTTP_CLIENTS.get()
    if sink is not None:
        sink.append(model)
    return model


def _adapter_model(model: str | None, adapter: Any):
    """Chat model apontado pro servidor do adapter, com o peso NO CORPO.

    O `model` do tier não vale aqui: um LoRA só existe colado no base com que foi
    treinado, então quem nomeia o modelo é `served_model`. `base_url` explícito
    porque o servidor MLX é outro processo, noutra porta, e o env do LM Studio
    aponta pro 1234. O sampling do card (temperature/top_p/max_tokens) ganha do
    `MODEL_TEMPERATURE` genérico: o adapter foi medido com ele, o default não.

    `adapters` é o path do diretório do peso — o servidor troca com um reload
    (~1,2s) e mantém um prompt cache por (modelo, adapter), sem contaminação
    entre runs. Falha em montar o cliente cai pro caminho sem adapter: a base
    atende, e derrubar um run já roteado por causa disto seria pior.
    """
    extra_body: dict[str, Any] = {
        "adapters": adapter.ref,
        "chat_template_kwargs": {"enable_thinking": bool(adapter.enable_thinking)},
    }
    if adapter.repeat_penalty is not None:
        extra_body["repetition_penalty"] = adapter.repeat_penalty
    kwargs: dict[str, Any] = {
        "base_url": _mlx_base_url() if adapter.runtime == RUNTIME_MLX else _lmstudio_base_url(),
        "extra_body": extra_body,
        "temperature": MODEL_TEMPERATURE if adapter.temperature is None else adapter.temperature,
    }
    if adapter.top_p is not None:
        kwargs["top_p"] = adapter.top_p
    if adapter.max_tokens is not None:
        kwargs["max_tokens"] = adapter.max_tokens
    try:
        from langchain.chat_models import init_chat_model

        return _track(init_chat_model(f"{OPENAI_PREFIX}{adapter.served_model}", **kwargs))
    except Exception:
        return _model_for(model)


def _prompt_files(prompt: str) -> list[str]:
    """Paths de arquivo citados no prompt, em ordem de aparição e sem repetir.

    Heurística de propósito: token com extensão conhecida. Falso positivo custa
    no máximo uma skill a mais no topo da fila (o `limit` do select segue
    valendo), e o eixo nem roda quando a lista sai vazia."""
    vistos: dict[str, None] = {}
    for match in PROMPT_FILE_RE.finditer(prompt):
        vistos.setdefault(match.group(0), None)
    return list(vistos)


def _skills_index(skills: list[Any]) -> str:
    """Índice das skills para o system prompt: nome — descrição, sem corpo.

    O corpo é conteúdo minerado pelo loop (dado); o índice é a única parte que
    o executor precisa ter com autoridade nossa — saber que a skill existe."""
    if not skills:
        return ""
    linhas = [f"- {s.name} — {s.description}".rstrip(" —") for s in skills]
    return "## Skills disponíveis\n" + "\n".join(linhas)


def _selected_skills(req: ExecRequest) -> list[Any]:
    """Seleção de skills deste request — determinística, então o `_build_agent`
    e o `_payload_messages` chegam na mesma lista sem passar estado entre eles
    (assinatura do `_build_agent` fica de pé)."""
    return select_skills(
        getattr(req, "kind", None), query=req.prompt, files=_prompt_files(req.prompt)
    )


def _untrusted_block(req: ExecRequest) -> str | None:
    """Bloco `<untrusted_reference_data>` deste request, ou None se não há dado.

    O que entra: corpo das skills selecionadas e a memória episódica. Os dois
    saíram do system prompt em 2026-08-04 (ver harness/trust_boundary.py)."""
    skills = _selected_skills(req)
    return trust_boundary.build_untrusted_block(
        {
            "Skills (corpo)": render_prompt(skills),
            "Histórico de runs anteriores": _episodic_block(getattr(req, "kind", None), req.prompt),
        }
    )


def _payload_messages(req: ExecRequest) -> list[dict[str, str]]:
    """Mensagens do invoke: dado não confiável ANTES da tarefa, em mensagem
    própria.

    Mensagem separada e não concatenação: o limite entre "isto é referência" e
    "isto é o pedido" fica estrutural, não só textual. Com a fronteira
    desligada (ou sem dado nenhum) volta a mensagem única de antes."""
    if not trust_boundary.enabled():
        return [{"role": "user", "content": req.prompt}]
    block = _untrusted_block(req)
    if not block:
        return [{"role": "user", "content": req.prompt}]
    return [
        {"role": "user", "content": block},
        {"role": "user", "content": f"{trust_boundary.TASK_HEADER}\n{req.prompt}"},
    ]


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


def _target_constitution(workspace: Path) -> str:
    """AGENTS-exec.md (ou AGENTS.md) do repo-alvo, ou "" se nenhum existir.

    Fica no system prompt, lado CONFIÁVEL da fronteira (trust_boundary), e não
    no bloco não confiável das skills/recall: é arquivo versionado do repo-alvo
    — escrito por quem mantém o projeto e revisado em commit —, não texto que o
    loop produziu numa execução anterior. Sem isto o executor mexe no repo sem
    nunca ler a lei local dele.

    Fail-open no padrão do arquivo: workspace sem o arquivo, ilegível ou fora do
    alcance => "" e o run segue como antes."""
    for name in TARGET_CONSTITUTION_FILES:
        path = workspace / name
        try:
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not raw:
            continue
        if len(raw) > TARGET_CONSTITUTION_MAX_CHARS:
            raw = (
                raw[:TARGET_CONSTITUTION_MAX_CHARS]
                + f"\n\n[truncado em {TARGET_CONSTITUTION_MAX_CHARS} caracteres — "
                f"leia {name} com read_file se precisar do resto]"
            )
        header = f"## Constituição do projeto\n\nDe `{name}` do repositório: é lei local, siga."
        return f"{header}\n\n{raw}"
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


def _truncated(messages: list[Any]) -> bool:
    """A ÚLTIMA resposta do modelo morreu no teto de tokens?

    O sinal é o `finish_reason` do provider no `response_metadata` da mensagem
    (o ChatOpenAI/LM Studio põe lá junto com `model_name`/`token_usage`).
    Fail-open: metadata ausente, vazio ou de formato inesperado => False, ou
    seja, o comportamento de antes.
    """
    for m in reversed(messages):
        if getattr(m, "type", None) != "ai":
            continue
        meta = getattr(m, "response_metadata", None)
        if not isinstance(meta, dict):
            return False
        raw = meta.get("finish_reason") or meta.get("stop_reason")
        return str(raw).strip().lower() in TRUNCATED_FINISH_REASONS
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


def _with_timeout(
    fn: Callable[[], Any], timeout_s: float, on_timeout: Callable[[], None] | None = None
) -> Any:
    """Roda `fn` numa thread daemon e desiste depois de `timeout_s`.

    Thread daemon em vez de `ThreadPoolExecutor`: o executor registra um
    `atexit` que espera a thread, o que faria o processo travar no fim justamente
    no caso de timeout (o invoke não é interrompível).

    `on_timeout` roda ANTES de levantar: é a chance de derrubar por fora o que a
    thread abandonada está fazendo (ver `_abort_http`). Best-effort e fail-open —
    o timeout já foi decidido, abort que falha não muda o veredito.
    """
    box: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            box.put((True, fn()))
        except BaseException as exc:
            box.put((False, exc))

    threading.Thread(target=target, daemon=True).start()
    try:
        ok, value = box.get(timeout=timeout_s)
    except queue.Empty:
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception:
                pass
        raise TimeoutError(f"invoke passou de {timeout_s}s") from None
    if not ok:
        raise value
    return value


def _abort_http(models: Sequence[Any]) -> None:
    """Fecha o cliente HTTP dos chat models do request — o abort do invoke.

    A thread do invoke não é interrompível, mas o que ela está fazendo quando
    estoura o prazo é um `recv` no socket do servidor local. Fechar o
    `root_client` (o `openai.OpenAI` que o langchain-openai monta) fecha o pool
    do httpx, o `recv` morre em `APIConnectionError` e a thread desenrola em ~1s
    — medido contra um servidor que aceita a conexão e nunca responde.

    Sem isto a conexão fica pendurada até o timeout do PRÓPRIO cliente openai,
    que é 600s com 2 retries: meia hora de conexão viva no LM Studio por caso
    abandonado, e um runner por caso entope o servidor em poucos minutos.

    Só o cliente síncrono: `root_async_client.close()` é corrotina e o invoke
    daqui é síncrono. Fail-open como o resto do arquivo — provider sem
    `root_client` (anthropic, gemini) simplesmente não tem abort e cai no
    comportamento de antes.
    """
    for model in models:
        close = getattr(getattr(model, "root_client", None), "close", None)
        if close is None:
            continue
        try:
            close()
        except Exception:
            pass


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


def _clear_blocker(ws: Path) -> None:
    """Sidecar de blocker fora do caminho. Fail-open como o resto do arquivo."""
    try:
        from harness.backends.blocker_tools import clear_blocker

        clear_blocker(ws)
    except Exception:
        pass


def _read_blocker(ws: Path) -> tuple[str, str] | None:
    try:
        from harness.backends.blocker_tools import read_blocker

        return read_blocker(ws)
    except Exception:
        return None


def _result(
    req: ExecRequest,
    before: dict[str, tuple[int, int]],
    *,
    ok: bool,
    exit_reason: ExitReason,
    turns: int,
    usage: Any,
    after: dict[str, tuple[int, int]] | None = None,
    blocker: str | None = None,
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
        blocker=blocker,
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
