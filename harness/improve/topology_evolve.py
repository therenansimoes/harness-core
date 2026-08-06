"""Ação 'topology_kind': operadores estruturais sobre a topologia de um kind.

Espelho do `prompt_evolve`, um andar acima: em vez de mutar TEXTO de prompt,
muta a ESTRUTURA do run-graph — e só a seção `[kinds.<k>]` (by_kind), nunca a
topologia de topo. Mutar o corpo de um kind não pode quebrar o corpo dos
outros, então o `apply` compila TODOS os kinds declarados antes de escrever.

Operadores DETERMINÍSTICOS (rng seedado → mesma proposta), zero LLM, zero
rede. A diferença de fundo em relação ao `actions.propose_topology` (que só
sabe inserir `reflect` na topologia de topo): aqui a candidata passa pela
gramática de `topology_grammar.check` antes de existir. Candidata ilegal não
vira proposta ruim — vira `None`, e nada toca o disco em nenhum caminho.

O kind sem seção própria herda o default de topo: o primeiro operador
MATERIALIZA `[kinds.<k>]` como cópia do default mais a mudança. A partir daí o
kind tem corpo próprio e o default continua intocado.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from random import Random
from typing import Any

from harness.graph import by_kind, topology
from harness.improve import actions, root_dir
from harness.improve import topology_grammar as gram

ACTION = "topology_kind"

OPERATORS: frozenset[str] = frozenset(
    {"insert_node", "remove_node", "rewire_edge", "split_parallel"}
)

# Nó PAGO com fiação própria de config — a evolução estrutural não move nós
# que gastam dinheiro; quem liga/desliga é o humano via topology.toml. Fonte
# única em `topology_grammar` (já sai de `gram.INSERTABLE` lá); aliasado aqui
# pra filtrar explícito nos operadores, sem depender só da derivação de INSERTABLE.
EVOLVE_FROZEN = gram.EVOLVE_FROZEN


def _load_full(root: Path | str | None, spec_path: Path | str | None) -> dict:
    p = Path(spec_path) if spec_path is not None else root_dir(root) / actions.TOPOLOGY_FILE
    return tomllib.loads(p.read_text(encoding="utf-8"))


def _pairs(section: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [tuple(e) for e in section.get("edges", ())]


def _degrees(pairs: list[tuple[str, str]], node: str) -> tuple[int, int]:
    return (
        sum(1 for _, dst in pairs if dst == node),
        sum(1 for src, _ in pairs if src == node),
    )


def _splice_candidates(pairs: list[tuple[str, str]]) -> list[int]:
    """Arestas lineares que aceitam um nó no meio. Fora: START (a entrada é a
    cabeça da espinha, `check` recusa desvio) e o `record->END`, que é a saída
    única do grafo."""
    return [
        i
        for i, (src, dst) in enumerate(pairs)
        if src not in (topology.START_NAME, "gate") and dst != topology.END_NAME
    ]


def _insert_node(
    nodes: list[str], pairs: list[tuple[str, str]], rng: Random
) -> tuple[list[str], list[tuple[str, str]], str] | None:
    candidates = sorted(gram.INSERTABLE - EVOLVE_FROZEN - set(nodes))
    slots = _splice_candidates(pairs)
    if not candidates or not slots:
        return None
    node = rng.choice(candidates)
    idx = rng.choice(slots)
    src, dst = pairs[idx]
    new_pairs = [*pairs[:idx], (src, node), (node, dst), *pairs[idx + 1 :]]
    return [*nodes, node], new_pairs, f"insert:{node}@{src}->{dst}"


def _remove_node(
    nodes: list[str], pairs: list[tuple[str, str]], rng: Random
) -> tuple[list[str], list[tuple[str, str]], str] | None:
    """Só insertable com grau 1/1: religar src→dst é inequívoco. Nó de espinha
    ou terminal nunca sai — quem tira `verify` do grafo não está evoluindo."""
    candidates = [
        n
        for n in sorted(set(nodes) & (gram.INSERTABLE - EVOLVE_FROZEN))
        if _degrees(pairs, n) == (1, 1)
    ]
    if not candidates:
        return None
    node = rng.choice(candidates)
    src = next(s for s, d in pairs if d == node)
    dst = next(d for s, d in pairs if s == node)
    new_pairs = [(s, d) for s, d in pairs if node not in (s, d)]
    idx = next((i for i, (s, _) in enumerate(new_pairs) if s == src), len(new_pairs))
    new_pairs = [*new_pairs[:idx], (src, dst), *new_pairs[idx:]]
    return [n for n in nodes if n != node], new_pairs, f"remove:{node}@{src}->{dst}"


def _rewire_edge(
    nodes: list[str], pairs: list[tuple[str, str]], rng: Random
) -> tuple[list[str], list[tuple[str, str]], str] | None:
    """Redireciona uma aresta da espinha pro rank imediatamente seguinte. Só
    +1: pular etapa (execute direto no gate) é mudança de política, não de
    estrutura, e a gramática não tem como julgar isso."""
    by_rank: dict[int, list[str]] = {}
    for n in nodes:
        if n in gram.SPINE_RANK:
            by_rank.setdefault(gram.SPINE_RANK[n], []).append(n)
    options = [
        (i, dst_new)
        for i, (src, dst) in enumerate(pairs)
        if src in gram.SPINE_RANK
        for dst_new in sorted(by_rank.get(gram.SPINE_RANK[src] + 1, ()))
        if dst_new != dst
    ]
    if not options:
        return None
    idx, dst_new = rng.choice(options)
    src, dst_old = pairs[idx]
    new_pairs = [*pairs[:idx], (src, dst_new), *pairs[idx + 1 :]]
    return nodes, new_pairs, f"rewire:{src}->{dst_new} (era {dst_old})"


def _split_parallel(
    nodes: list[str], pairs: list[tuple[str, str]], rng: Random
) -> tuple[list[str], list[tuple[str, str]], str] | None:
    """Diamante: src→dst vira src→{n1,n2}→dst. O par sai de
    `EVENTS_ONLY_SAFE` — só nó cujo contrato é escrever exclusivamente `events`
    pode rodar concorrente, porque é a única chave do RunState com reducer.
    Menos de dois disponíveis (o caso do repo hoje, sem plugin aprovado) é
    None, não um diamante degenerado com o mesmo nó nos dois ramos."""
    available = sorted(gram.EVENTS_ONLY_SAFE() - set(nodes))
    slots = _splice_candidates(pairs)
    if len(available) < 2 or not slots:
        return None
    n1, n2 = sorted(rng.sample(available, 2))
    idx = rng.choice(slots)
    src, dst = pairs[idx]
    new_pairs = [*pairs[:idx], (src, n1), (src, n2), (n1, dst), (n2, dst), *pairs[idx + 1 :]]
    return [*nodes, n1, n2], new_pairs, f"split:{n1}|{n2}@{src}->{dst}"


_APPLY = {
    "insert_node": _insert_node,
    "remove_node": _remove_node,
    "rewire_edge": _rewire_edge,
    "split_parallel": _split_parallel,
}


def render(full_spec: Mapping[str, Any]) -> str:
    """Spec inteira -> texto TOML, round-trip garantido. Delega no render do
    `actions` justamente pra não haver dois formatos de topology.toml."""
    return actions.render_topology(full_spec)


def propose(
    kind: str,
    operator: str,
    rng: Random,
    root: Path | str | None = None,
    genome: Any = None,
    spec_path: Path | str | None = None,
) -> actions.TopologyProposal | None:
    """Operador -> candidata -> gramática. Ilegal (ou operador sem material)
    devolve None: o proponente não escreve, não levanta e não deixa rastro.

    `genome` viaja pro `apply` (é lá que o fail-closed mora, como no
    `actions.apply_topology`); aqui ele só existe pra assinatura casar com o
    resto do registry.
    """
    if operator not in OPERATORS:
        raise KeyError(
            f"operador desconhecido: {operator!r} (disponíveis: {', '.join(sorted(OPERATORS))})"
        )
    full = _load_full(root, spec_path)
    section = by_kind.resolve_spec(full, kind)
    nodes = list(section.get("nodes") or ())
    pairs = _pairs(section)

    out = _APPLY[operator](nodes, pairs, rng)
    if out is None:
        return None
    new_nodes, new_pairs, reason = out
    candidate = {"nodes": new_nodes, "edges": [list(e) for e in new_pairs]}
    if gram.check(candidate):
        return None

    kinds = dict(full.get(by_kind.KINDS_KEY) or {})
    kinds[kind] = candidate
    new_full = {**full, by_kind.KINDS_KEY: kinds}
    return actions.TopologyProposal(
        target_file=actions.TOPOLOGY_FILE,
        new_text=render(new_full),
        reasons=(f"{operator}:{kind}", reason),
    )


def apply(
    proposal: actions.TopologyProposal,
    root: Path | str | None = None,
    genome: Any = None,
) -> Path:
    """Compila TODOS os kinds declarados antes de delegar: a proposta muda um
    kind, mas o arquivo é um só e o run de outro kind não pode virar fallback
    silencioso por causa disso. Escrita (e genoma) continuam no `actions`."""
    spec = tomllib.loads(proposal.new_text)
    for kind in by_kind.kinds_declared(spec):
        topology.compile_spec(by_kind.resolve_spec(spec, kind))
    return actions.apply_topology(proposal, root=root, genome=genome)


def action():
    """A ação registrável. O wiring no registry é de outra fatia — devolver a
    Action já pronta permite testar propose/apply sem tocar `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose, apply=apply)
