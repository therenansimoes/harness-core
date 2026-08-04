"""`declare_blocker`: o modelo DIZ por que não conclui, e o gate roteia melhor.

O buraco medido é o do exit_reason genérico. Run que para porque falta uma
credencial, run que para porque o serviço externo ainda não respondeu e run que
para porque o agente não entendeu o objetivo saem todos iguais do backend
("stalled", "done" sem escrita, "max_turns") — e o gate trata os três do mesmo
jeito: mais uma tentativa pelo mesmo caminho. Duas delas são retry desperdiçado.

Aqui o motivo é DECLARADO, com vocabulário fechado (`TYPES`), e vira sinal de
roteamento: `needs_user_input` é rota pro humano (não queima tentativa),
`external_wait` é retry adiado, os outros dois são retry normal.

O canal é um sidecar em `<ws>/.harness/blocker.json` porque a tool roda dentro
do grafo do agente e o backend precisa da declaração DEPOIS do invoke — estado de
mensagem não sobrevive à leitura do resultado, arquivo sobrevive. Escrita atômica
(tmp irmão + `os.replace`) pelo motivo de sempre: nunca existe sidecar meio
escrito para o backend ler.

Tipo fora de `TYPES` NÃO grava: devolve string de erro com os válidos, e o modelo
tem a chance de declarar de novo com o vocabulário certo. Inventar tipo aqui seria
o gate roteando por texto livre.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BLOCKER_JSON = ".harness/blocker.json"

# Vocabulário fechado. A ordem é a do verbete no manual das tools.
TYPES = (
    "missing_evidence",
    "needs_user_input",
    "external_wait",
    "goal_not_met_yet",
)

MAX_DETAIL = 1000


def blocker_path(ws: str | Path) -> Path:
    return Path(ws) / BLOCKER_JSON


def read_blocker(ws: str | Path) -> tuple[str, str] | None:
    """`(type, detail)` do sidecar, ou None. Sidecar torto = None, nunca exceção.

    Tipo fora de `TYPES` também é None: sidecar escrito à mão (ou por versão
    velha) não pode virar rota de gate que ninguém previu.
    """
    try:
        data = json.loads(blocker_path(ws).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tipo = data.get("type")
    if tipo not in TYPES:
        return None
    return str(tipo), str(data.get("detail") or "")


def clear_blocker(ws: str | Path) -> None:
    """Apaga o sidecar. Chamado no começo de cada tentativa: blocker declarado na
    anterior não pode vazar para o exit_reason desta."""
    try:
        blocker_path(ws).unlink(missing_ok=True)
    except OSError:  # ws somente-leitura não derruba o run
        pass


def write_blocker(ws: str | Path, tipo: str, detail: str) -> None:
    """tmp no MESMO diretório + `os.replace`: nunca existe sidecar meio escrito."""
    target = blocker_path(ws)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"type": tipo, "detail": detail[:MAX_DETAIL]}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def _tipos() -> str:
    return ", ".join(TYPES)


def make_blocker_tools(ws: str | Path) -> list:
    """Tools LangChain deste módulo com o workspace fixado."""
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    # `type` shadowa o builtin de propósito: é o nome do parâmetro no contrato
    # da tool (o schema que o modelo vê sai da assinatura).
    def _declare_blocker(type: str, detail: str) -> str:
        """Declara por que você não consegue concluir, com tipo e detalhe."""
        tipo = (type or "").strip()
        if tipo not in TYPES:
            return (
                f"declare_blocker: tipo '{type}' não existe. Use um destes: {_tipos()}. "
                "Nada foi registrado — chame de novo com o tipo certo."
            )
        texto = (detail or "").strip()
        if not texto:
            return (
                "declare_blocker: `detail` vazio. Diga em uma frase o que falta e "
                "quem/o quê destrava — sem isso a declaração não ajuda ninguém."
            )
        try:
            write_blocker(base, tipo, texto)
        except OSError as exc:  # tool node não pode receber exceção
            return f"declare_blocker falhou ao gravar: {exc}"
        return (
            f"blocker '{tipo}' registrado. Pare de tentar contornar: escreva o que já "
            "deu para escrever e encerre o turno."
        )

    return [
        StructuredTool.from_function(
            func=_declare_blocker,
            name="declare_blocker",
            description=(
                "Declara POR QUE você não consegue concluir a tarefa. `type` é um de: "
                f"{_tipos()}. `detail` é uma frase dizendo o que falta. "
                "Use quando parar for a decisão certa: sem isto sua parada chega ao "
                "gate como 'não fez nada' e você ganha outra tentativa pelo mesmo "
                "caminho morto. `needs_user_input` NÃO é desistência — é o pedido de "
                "decisão humana, e é o único caminho que não gasta tentativa. "
                "`external_wait` faz a próxima tentativa esperar antes de rodar. "
                "Declare UMA vez, escreva o que já é possível escrever, e encerre."
            ),
        ),
    ]


__all__ = [
    "BLOCKER_JSON",
    "TYPES",
    "blocker_path",
    "clear_blocker",
    "make_blocker_tools",
    "read_blocker",
    "write_blocker",
]
