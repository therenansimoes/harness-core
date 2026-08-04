"""Middleware que avisa o modelo quando ele repete a MESMA tool call.

O modo de falha é caro e silencioso: executor pequeno chama `edit_file` com
`old_string` idêntico, leva "String not found", e chama de novo — igual — até o
limite de turnos acabar. Nada no stack reclama: cada call é válida
individualmente, e o retry de infra não vê nada (a tool respondeu).

A guarda conta hash de `(nome, args)` numa janela curta e, na terceira
repetição idêntica, cola um aviso no fim do tool result. É aviso, não bloqueio:
recusar a call gastaria o mesmo turno e tiraria do modelo a chance de ler o
resultado. Depois de avisar a janela é ZERADA — sem isso a quarta, quinta e
sexta calls ganhariam o mesmo aviso e o contexto viraria eco.

Posição no stack importa: este middleware entra ANTES do `ToolRetryMiddleware`
(primeiro = mais externo), então ele conta as calls que o MODELO fez. Se
entrasse por dentro, cada retry de rede contaria como repetição do modelo e o
aviso sairia por falha de infra, que não é culpa de abordagem nenhuma.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

# Janela de calls olhadas. Curta de propósito: repetição que interessa é a
# teimosia imediata, não um `read_file` no mesmo arquivo vinte turnos depois.
WINDOW = 8
# Duas calls iguais ainda podem ser tentativa legítima (ler, editar, reler).
# Três é padrão.
THRESHOLD = 3

NOTICE = (
    "\n\n[loop_guard] Você repetiu {tool} com os mesmos argumentos {n}x — "
    "mude a abordagem ou finalize a tarefa."
)


def _chave(tool_call: Any) -> str:
    """Hash curto de nome + argumentos, estável entre calls iguais.

    `sort_keys` porque a ordem das chaves do dict de args vem do JSON do
    provider e não é garantida; `default=str` porque arg não serializável não
    pode virar exceção dentro do caminho da tool.
    """
    nome = tool_call.get("name") or ""
    args = json.dumps(tool_call.get("args") or {}, sort_keys=True, default=str)
    return hashlib.sha1(f"{nome}{args}".encode()).hexdigest()[:12]


def _append_notice(result: Any, notice: str) -> Any:
    """Cola o aviso no fim do conteúdo textual (mesmo padrão do `smart_fs`).

    `Command` (tool que devolve update de estado) passa intocado: não existe
    campo de texto para anexar sem inventar mensagem no grafo.
    """
    if isinstance(result, ToolMessage) and isinstance(result.content, str):
        result.content = result.content + notice
    return result


class LoopGuardMiddleware(AgentMiddleware):
    """Conta tool calls idênticas na janela e avisa na `THRESHOLD`-ésima."""

    def __init__(self) -> None:
        super().__init__()
        self._recentes: deque[str] = deque(maxlen=WINDOW)

    def wrap_tool_call(self, request, handler):
        res = handler(request)
        return self._registra(request, res)

    async def awrap_tool_call(self, request, handler):
        # Mesmo corpo do síncrono: a base levanta NotImplementedError no caminho
        # que a subclasse não implementa, então `ainvoke`/`astream` ficaria sem
        # guarda se só existisse o sync.
        res = await handler(request)
        return self._registra(request, res)

    def _registra(self, request, res: Any) -> Any:
        """Contabiliza a call e devolve `res` — com ou sem aviso.

        Qualquer erro aqui é engolido: uma guarda de heurística não tem direito
        de derrubar o tool node e matar o run.
        """
        try:
            chave = _chave(request.tool_call)
            self._recentes.append(chave)
            repeticoes = self._recentes.count(chave)
            if repeticoes < THRESHOLD:
                return res
            # Cooldown: sem isto toda call seguinte da janela reavisa.
            self._recentes.clear()
            nome = request.tool_call.get("name") or "essa tool"
            return _append_notice(res, NOTICE.format(tool=f"`{nome}`", n=repeticoes))
        except Exception:
            return res


__all__ = ["THRESHOLD", "WINDOW", "LoopGuardMiddleware"]
