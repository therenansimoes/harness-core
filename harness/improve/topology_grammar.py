"""Gramática da topologia como DADO EM CÓDIGO.

`topology._validate` é fail-closed mas SINTÁTICO: whitelist, obrigatórios,
arestas bem formadas. Ele aceita grafo que compila e não roda — `verify` antes
de `execute`, nó órfão, dois caminhos pro END. Quem propõe estrutura sozinho
(topology_evolve) precisa de um juiz mais duro que isso, e esse juiz é aqui:
a ORDEM do pipeline vira rank, e aresta que anda pra trás sem ser a perna do
retry é ilegal.

O default REAL do repo (`config/topology.toml`) é a referência de legalidade:
espinha plan→route→provision→execute→verify→measure→gate, saídas do gate
condicionais no código, e o retry voltando pra `route` — hoje passando pelo
checker `reflect` (retry→reflect→route). Os dois formatos da perna do retry
(direto e via nó insertable) são legais.

`check` devolve LISTA de motivos (vazia = legal) em vez de levantar: o
proponente descarta candidata em silêncio e precisa do motivo pro rastro, não
de um try/except por operador.
"""

from __future__ import annotations

from typing import Any, Mapping

from harness.graph import topology

# Ordem do pipeline. Rank só existe pra espinha: nó de fora (terminal do gate,
# insertable) não tem posição fixa e por isso não entra na regra de inversão.
SPINE_RANK: dict[str, int] = {
    "plan": 0,
    "route": 1,
    "provision": 2,
    "execute": 3,
    "verify": 4,
    "measure": 5,
    "gate": 6,
}

SPINE = frozenset(SPINE_RANK)
# Alvos do gate + o registro: fim de caminho, nunca meio de aresta linear.
TERMINAL = frozenset({"accept", "retry", "escalate", "revert", "record"})

# Aresta pra trás declarada legal. Só a perna do retry: `retry` volta pra
# `route` pra refazer a tentativa. A forma com checker no meio
# (retry→reflect→route, o default do repo) é legal por construção — `retry` e
# `reflect` não têm rank, então a regra de inversão nem se aplica a elas.
BACK_EDGES = frozenset({("retry", "route")})

# Whitelist de inserção: o que sobra da espinha e dos terminais. Derivado do
# NODE_IMPLS de propósito — nó novo no runtime entra aqui sem editar isto.
INSERTABLE = frozenset(topology.NODE_IMPLS) - SPINE - TERMINAL - {"gate"}


def insertable() -> frozenset[str]:
    """Nós que um operador estrutural pode inserir/remover."""
    return INSERTABLE


def entry_node(nodes: list[str]) -> str | None:
    """Nó de menor rank presente — o único destino legal de START."""
    ranked = [n for n in nodes if n in SPINE_RANK]
    return min(ranked, key=lambda n: SPINE_RANK[n]) if ranked else None


def _reachable(nodes: list[str], pairs: list[tuple[str, str]]) -> set[str]:
    """Alcançáveis de START. As saídas do gate são condicionais e vivem no
    código (`_after_gate`), então entram no grafo aqui na mão — sem isso todo
    terminal pareceria órfão."""
    adj: dict[str, list[str]] = {}
    for src, dst in pairs:
        adj.setdefault(src, []).append(dst)
    if "gate" in nodes:
        adj.setdefault("gate", []).extend(t for t in topology.GATE_TARGETS if t in nodes)
    seen: set[str] = set()
    stack = [topology.START_NAME]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur, ()))
    return seen


def check(spec: Mapping[str, Any]) -> list[str]:
    """Motivos que tornam a spec ilegal; lista vazia = legal.

    Recebe uma seção resolvida (nodes/edges), não o toml inteiro — o mesmo que
    `by_kind.resolve_spec` devolve.
    """
    try:
        nodes, pairs = topology._validate(spec)
    except topology.TopologyError as exc:
        return [f"validate: {exc}"]

    reasons: list[str] = []

    # (e) obrigatórios e alvos do gate. `_validate` já barra, mas `check` tem
    # que valer sozinho: é ele que o proponente chama.
    missing = sorted(topology.REQUIRED_NODES - set(nodes))
    if missing:
        reasons.append(f"obrigatório ausente: {missing}")
    absent_targets = [t for t in topology.GATE_TARGETS if t not in nodes]
    if absent_targets:
        reasons.append(f"alvo do gate ausente: {absent_targets}")

    # (a) START entra pela cabeça da espinha, não pelo meio.
    entry = entry_node(nodes)
    for src, dst in pairs:
        if src == topology.START_NAME and dst != entry:
            reasons.append(f"START->{dst}: entrada tem que ser {entry!r}")

    # (b) sem andar pra trás na espinha fora das back-edges declaradas.
    for src, dst in pairs:
        if src in SPINE_RANK and dst in SPINE_RANK:
            if SPINE_RANK[src] > SPINE_RANK[dst] and (src, dst) not in BACK_EDGES:
                reasons.append(f"{src}->{dst}: rank invertido")

    # (c) nó declarado e inalcançável é peso morto que ninguém revisou.
    orphans = sorted(set(nodes) - _reachable(nodes, pairs))
    if orphans:
        reasons.append(f"inalcançável de START: {orphans}")

    # (d) uma saída só, e por `record`: o ledger não pode ser contornado.
    to_end = [src for src, dst in pairs if dst == topology.END_NAME]
    if to_end != ["record"]:
        reasons.append(f"saída pro END tem que ser só record->END (achado: {to_end})")

    return reasons
