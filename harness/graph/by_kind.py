"""Topologia por kind: um genoma, várias formas de corpo.

`config/topology.toml` continua tendo UMA topologia de topo (`nodes`/`edges`) —
essa é o default global e o que todo run usava. O que muda aqui: uma seção
opcional `[kinds.<kind>]` pode declarar OUTRA topologia inteira para unidades
daquele kind (o rótulo de `harness/routing/kinds.py`). Conteúdo pode querer um
grafo sem provision; refactor pode querer dois verifies. Isso vira dado, não
código de nó.

Duas regras que valem a leitura:

- **Nunca merge.** A seção de um kind substitui a topologia inteira ou não
  existe. Merge parcial produz grafo que ninguém escreveu e ninguém revisou —
  seção com só `nodes` (ou só `edges`) é TopologyError, fail-closed, igual à
  validação do topology.
- **Fail-open só na borda.** `build_for_unit` é a única função que engole
  exceção: qualquer coisa torta (toml ausente, seção parcial, spec inválida,
  erro do langgraph) vira 1 linha no stderr e cai no `build_run_graph`, que por
  sua vez tem o próprio fallback para a topologia embutida. O run não morre por
  causa de config.

Kind declarado que não existe em VALID_KINDS é aceito e ignorado: o resolve só
casa contra o kind da unidade, então seção órfã é lixo inerte, não erro.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from harness.graph import run_graph as _rg
from harness.graph import topology

KINDS_KEY = "kinds"


def resolve_spec(spec: Mapping[str, Any], kind: str | None) -> dict:
    """Escolhe a topologia do kind, ou a de topo. Sempre devolve dict com
    exatamente nodes/edges — a validação de conteúdo é do topology."""
    section = (spec.get(KINDS_KEY) or {}).get(kind) if kind else None
    if not isinstance(section, Mapping):
        return {"nodes": spec.get("nodes"), "edges": spec.get("edges")}
    has_nodes = "nodes" in section
    has_edges = "edges" in section
    if not (has_nodes and has_edges):
        raise topology.TopologyError(
            f"{KINDS_KEY}.{kind}: seção parcial (precisa nodes e edges)"
        )
    return {"nodes": section["nodes"], "edges": section["edges"]}


def load_for_kind(kind: str | None, path=None) -> dict:
    """Lê o toml e resolve. Erro de leitura sobe — fallback é do chamador."""
    return resolve_spec(topology.load_spec(path), kind)


def kinds_declared(spec: Mapping[str, Any]) -> list[str]:
    """Kinds com seção própria na spec (inclui os que não são Kind válido)."""
    section = spec.get(KINDS_KEY)
    return sorted(section) if isinstance(section, Mapping) else []


def build_for_unit(unit, checkpointer):
    """Grafo compilado para a unidade. Única borda fail-open do módulo."""
    kind = getattr(unit, "kind", None)
    try:
        # nós de plugin aprovados entram na whitelist antes do compile
        from harness.graph import plugin_nodes

        plugin_nodes.register_all()
    except Exception:
        pass
    try:
        return topology.compile_spec(load_for_kind(kind), checkpointer)
    except Exception as exc:
        print(
            f"by_kind: topologia de kind={kind!r} ignorada ({exc}); topologia default",
            file=sys.stderr,
        )
    return _rg.build_run_graph(checkpointer)
