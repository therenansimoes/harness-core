"""Topologia do run-graph como dado (estilo ADAS).

`config/topology.toml` (zona mutável do genoma) declara nós e arestas; o
código dos nós continua imutável em run_graph. O loop de auto-melhoria pode
propor mudança de ESTRUTURA editando o toml e provar no A/B — nunca editando
nó. Validação fail-closed: spec torta => TopologyError, nunca grafo
meio-válido; quem decide o fallback é o chamador (build_run_graph).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping

from harness.graph import reflect as _reflect_mod
from harness.graph import run_graph as _rg
from harness.graph.state import RunState
from harness.routing import config_dir

TOPOLOGY_TOML = "topology.toml"
START_NAME = "START"
END_NAME = "END"

# Sem estes o run não tem espinha nem registro — spec sem eles é recusada.
REQUIRED_NODES = frozenset({"plan", "execute", "verify", "gate", "record"})
# Alvos das arestas condicionais do gate. A decisão vem de ruler/gate e o
# mapeamento decisão->nó é código (_after_gate); a spec não redireciona.
GATE_TARGETS = ("accept", "retry", "escalate", "revert")


class TopologyError(ValueError):
    """Spec de topologia inválida."""


def _reflect(state: RunState, config=None) -> dict:
    """Checker do retry: lê o rastro da tentativa reprovada e deixa um hint no
    estado para o prompt da tentativa seguinte (`run_graph._prompt`).

    Determinístico e $0. Fora do caminho do retry (ex.: reflect entre plan e
    route) não há tentativa morta para ler e o hint sai vazio — nó continua
    sendo pass-through, como quando era só evento.
    """
    hint = _reflect_mod.build_hint(_reflect_mod.hydrate(state, _rg._db(config)))
    return {
        "reflect_hint": hint,
        "events": [_rg._event("reflect", hint=bool(hint))],
    }


# Whitelist: só estes nomes podem aparecer na spec.
NODE_IMPLS = {
    "plan": _rg._plan,
    "route": _rg._route,
    "provision": _rg._provision,
    "execute": _rg._execute,
    "verify": _rg._verify,
    "measure": _rg._measure,
    "gate": _rg._gate,
    "accept": _rg._accept,
    "retry": _rg._retry,
    "escalate": _rg._escalate,
    "revert": _rg._revert,
    "record": _rg._record,
    "reflect": _reflect,
}


def default_spec_path() -> Path:
    return config_dir() / TOPOLOGY_TOML


def load_spec(path: Path | None = None) -> dict:
    """Lê o toml. Ausente/malformado sobe exceção — fallback é do chamador."""
    p = Path(path) if path is not None else default_spec_path()
    return tomllib.loads(p.read_text(encoding="utf-8"))


def _validate(spec: Mapping[str, Any]) -> tuple[list[str], list[tuple[str, str]]]:
    nodes = spec.get("nodes")
    edges = spec.get("edges")
    if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
        raise TopologyError("'nodes' precisa ser lista de strings")
    if len(set(nodes)) != len(nodes):
        raise TopologyError("'nodes' tem duplicata")
    unknown = sorted(set(nodes) - set(NODE_IMPLS))
    if unknown:
        raise TopologyError(
            f"impl desconhecida: {unknown}; whitelist: {sorted(NODE_IMPLS)}"
        )
    missing = sorted(REQUIRED_NODES - set(nodes))
    if missing:
        raise TopologyError(f"nós obrigatórios ausentes: {missing}")
    for t in GATE_TARGETS:
        if t not in nodes:
            raise TopologyError(f"alvo condicional do gate ausente: {t!r}")
    if not isinstance(edges, list):
        raise TopologyError("'edges' precisa ser lista de pares [origem, destino]")
    pairs: list[tuple[str, str]] = []
    for e in edges:
        if not (
            isinstance(e, list) and len(e) == 2 and all(isinstance(x, str) for x in e)
        ):
            raise TopologyError(f"aresta inválida (esperado [origem, destino]): {e!r}")
        src, dst = e
        if src == "gate":
            raise TopologyError(
                "aresta linear saindo de 'gate' é proibida: as saídas do gate "
                "são condicionais e vivem no código"
            )
        if src != START_NAME and src not in nodes:
            raise TopologyError(f"aresta parte de nó não declarado: {src!r}")
        if dst != END_NAME and dst not in nodes:
            raise TopologyError(f"aresta chega em nó não declarado: {dst!r}")
        pairs.append((src, dst))
    if not any(src == START_NAME for src, _ in pairs):
        raise TopologyError("falta aresta saindo de START")
    return list(nodes), pairs


def build(spec: Mapping[str, Any]):
    """Spec validada -> StateGraph (não compilado)."""
    from langgraph.graph import END, START, StateGraph

    nodes, pairs = _validate(spec)
    b = StateGraph(RunState)
    for name in nodes:
        b.add_node(name, NODE_IMPLS[name])
    for src, dst in pairs:
        b.add_edge(
            START if src == START_NAME else src, END if dst == END_NAME else dst
        )
    b.add_conditional_edges("gate", _rg._after_gate, list(GATE_TARGETS))
    return b


def compile_spec(spec: Mapping[str, Any], checkpointer=None):
    """Spec -> grafo compilado. Erros do langgraph também sobem — fail-closed."""
    return build(spec).compile(checkpointer=checkpointer)
