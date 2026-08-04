"""Papéis de subagent para o executor deepagents, lidos de config/agents.toml.

Definição de papel é DADO na zona mutável do genoma (`config/*.toml`): o loop
cria e calibra papel sem tocar em código. Contrato igual ao de `mcp_tools`:
`load_roles` NUNCA levanta — arquivo ausente, toml quebrado, papel torto ou
`deepagents` desinstalado viram `[]`/papel ignorado com uma linha no stderr, e
o executor volta ao comportamento de antes (só o `general-purpose` default da
tool `task`). O import da lib é lazy (regra do repo: LangChain nunca no topo).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = "config/agents.toml"


def load_roles(
    config_path: str | Path | None = None,
    *,
    backend: Any = None,
    allowed: Any = (),
    model: Any = None,
) -> list[dict]:
    """Specs de subagent no formato que `create_deep_agent(subagents=...)` aceita.

    `backend` é o filesystem do run: sem ele não há como restringir as tools do
    papel (a restrição é um `FilesystemMiddleware` próprio, mesmo mecanismo do
    agente principal), então a allowlist fica só no prompt do papel. `allowed` é
    a allowlist real do run — vazia significa "sem restrição no principal".
    `model` é o modelo do run, necessário só para papel com `delegates_to`: o
    subagent aninhado é compilado na hora (ver `_delegacao`)."""
    try:
        path = Path(config_path or DEFAULT_CONFIG)
        if not path.is_file():
            return []

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        declarado = data.get("agents") or {}
        roles = []
        for name, cfg in declarado.items():
            spec = _spec(name, cfg, backend, list(allowed or ()))
            if spec:
                roles.append(spec)
        return _liga_delegacao(roles, declarado, backend, model)
    except Exception as exc:  # broad de propósito: papel é opcional, nunca derruba o run
        print(f"agent_roles: falha ao carregar papéis: {exc}", file=sys.stderr)
        return []


def roles_manual(roles: list[dict]) -> str:
    """Uma linha por papel para o system prompt, ou "" se não há papel.

    Sem isto o modelo tem a tool `task` e nenhuma pista de quando usá-la."""
    if not roles:
        return ""
    linhas = [
        f"- task(subagent_type={r['name']!r}) — {r['description']}"
        for r in roles
        if r.get("name") and r.get("description")
    ]
    if not linhas:
        return ""
    return (
        "Você pode delegar micro tarefas com a tool `task`, uma por chamada:\n"
        + "\n".join(linhas)
        + "\nDelegue só quando ajuda (tarefa multi-arquivo, conferência final); "
        "tarefa de um arquivo você mesmo faz."
    )


def _spec(name: str, cfg: Any, backend: Any, allowed: list[str]) -> dict | None:
    """Um papel do toml em spec de SubAgent, ou None se o papel não serve."""
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    description = str(cfg.get("description", "")).strip()
    prompt = str(cfg.get("prompt", "")).strip()
    if not description or not prompt:
        print(
            f"agent_roles: papel {name!r} sem description/prompt, ignorado",
            file=sys.stderr,
        )
        return None

    tools = _tools(cfg.get("tools", []), allowed)
    if allowed and not tools:
        # Papel que não sobrevive à allowlist do run some: herdar o stack
        # completo daria ao subagent mais permissão que o agente principal.
        print(
            f"agent_roles: papel {name!r} sem tool permitida neste run, ignorado",
            file=sys.stderr,
        )
        return None

    spec: dict[str, Any] = {
        "name": name,
        "description": description,
        # `SubAgent` chama de `system_prompt`; o toml fala `prompt` porque quem
        # escreve é o loop, não a lib.
        "system_prompt": _prompt_com_allowlist(prompt, tools),
    }
    model = _model(cfg.get("model"))
    if model:
        # `SubAgent.model` é `str | BaseChatModel`; string `provider:modelo` é o
        # que a lib resolve sozinha. A chave só entra quando o papel pediu outro
        # modelo — ausente significa "herda o do agente principal", e é assim
        # que fica enquanto a máquina serve um modelo por vez.
        spec["model"] = model
    middleware = _fs_middleware(backend, tools)
    if middleware:
        spec["middleware"] = middleware
    return spec


def _liga_delegacao(roles: list[dict], declarado: dict, backend: Any, model: Any) -> list[dict]:
    """Equipa com `task` o papel que declarou `delegates_to` — e só ele.

    GARANTIA ANTI-RECURSÃO: a lista de alvos exclui o próprio papel e qualquer
    outro delegador, então o conductor alcança planner/reviewer e NENHUM papel
    alcança o conductor. Não depende de prompt: subagent declarativo da lib não
    ganha `task` nenhuma (o stack default dele é filesystem + summarization +
    patch_tool_calls, ver a montagem de `subagent_middleware` em
    `deepagents/graph.py`), logo a única `task` que existe dentro de um papel é a
    que nós montamos aqui, com a lista já filtrada.

    Papel que pediu delegação e não consegue (sem `model` do run, sem alvo vivo,
    lib torta) SAI da lista: conductor sem `task` não faz o que a description
    promete e o modelo principal delegaria a um papel manco."""
    delegadores = {
        name for name, cfg in declarado.items() if isinstance(cfg, dict) and cfg.get("delegates_to")
    }
    if not delegadores:
        return roles

    por_nome = {r["name"]: r for r in roles}
    vivos = []
    for spec in roles:
        name = spec["name"]
        if name not in delegadores:
            vivos.append(spec)
            continue
        alvos = [
            por_nome[alvo]
            for alvo in declarado[name]["delegates_to"]
            if isinstance(alvo, str) and alvo in por_nome and alvo not in delegadores
        ]
        middleware = _task_middleware(alvos, backend, model)
        if not middleware:
            print(
                f"agent_roles: papel {name!r} não conseguiu delegar, ignorado",
                file=sys.stderr,
            )
            continue
        spec["middleware"] = list(spec.get("middleware") or []) + middleware
        vivos.append(spec)
    return vivos


def _task_middleware(alvos: list[dict], backend: Any, model: Any) -> list[Any]:
    """`SubAgentMiddleware` com a lista FECHADA de alvos, ou [] se não dá."""
    if not alvos:
        return []
    try:
        from deepagents.middleware.subagents import SubAgentMiddleware

        # A lib compila cada spec já na construção do middleware e
        # `create_sub_agent` exige `model` e `tools`. As tools de arquivo do alvo
        # vêm do `FilesystemMiddleware` dele (`tools=` só aceita objeto de tool,
        # não nome), então `[]` aqui é a lista aditiva vazia — não "sem tool".
        aninhados = []
        for alvo in alvos:
            spec = {**alvo, "tools": []}
            spec.setdefault("model", model)
            if not spec["model"]:
                return []
            aninhados.append(spec)
        return [SubAgentMiddleware(backend=backend, subagents=aninhados)]
    except Exception as exc:
        print(f"agent_roles: sem delegação no papel: {exc}", file=sys.stderr)
        return []


def _model(declared: Any) -> str | None:
    """Modelo do papel, ou None quando o papel herda o do agente principal.

    `"inherit"` (e ausente/vazio) é herdar: a chave `model` nem aparece na spec."""
    if not isinstance(declared, str):
        return None
    model = declared.strip()
    return None if model in ("", "inherit") else model


def _tools(declared: Any, allowed: list[str]) -> list[str]:
    """Tools do papel ∩ allowlist do run, na ordem declarada pelo papel."""
    names = [t for t in declared if isinstance(t, str)] if isinstance(declared, list) else []
    if allowed:
        names = [t for t in names if t in allowed]
    return names


def _prompt_com_allowlist(prompt: str, tools: list[str]) -> str:
    """A restrição também vai no prompt: sem `backend` o middleware não existe,
    e mesmo com ele o modelo pequeno tenta a tool que não tem e queima turno."""
    if not tools:
        return prompt
    return f"{prompt}\n\nSuas tools são só estas: {', '.join(tools)}."


def _fs_middleware(backend: Any, tools: list[str]) -> list[Any]:
    """`FilesystemMiddleware` do papel, ou [] se não dá para restringir.

    Mesmo mecanismo do agente principal: `tools=` do subagent é aditivo, quem
    restringe as tools de arquivo é substituir o middleware (merge por `.name`).
    Nome fora do `FsToolName` cai fora — a lib levantaria em request-time."""
    if backend is None or not tools:
        return []
    try:
        from typing import get_args

        from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName

        known = [t for t in tools if t in get_args(FsToolName)]
        if not known:
            return []
        return [FilesystemMiddleware(backend=backend, tools=known)]
    except Exception as exc:
        print(f"agent_roles: sem restrição de tools no papel: {exc}", file=sys.stderr)
        return []
