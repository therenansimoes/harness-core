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

Fan-out (dois caminhos saindo do mesmo nó) é o caso onde a gramática sabe algo
que o `_validate` não sabe: `RunState` tem reducer em `events` e em NENHUMA
outra chave, então dois nós no mesmo super-step que escrevam qualquer outra
coisa é `InvalidUpdateError` em runtime — grafo que compila e explode no
primeiro run. Daí a regra (f): paralelismo só entre nós cujo contrato é
escrever exclusivamente `events`, e só em diamante fechado.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harness.graph import plugin_nodes, topology

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

# Nó PAGO com fiação própria de config — a evolução estrutural não move nós
# que gastam dinheiro; quem liga/desliga é o humano via topology.toml.
EVOLVE_FROZEN = frozenset({"advise"})

# Whitelist de inserção: o que sobra da espinha e dos terminais. Derivado do
# NODE_IMPLS de propósito — nó novo no runtime entra aqui sem editar isto.
# EVOLVE_FROZEN sai daqui também: "insertable" tem que continuar significando
# "candidato de mutação estrutural", não só "fora da espinha/terminais".
INSERTABLE = frozenset(topology.NODE_IMPLS) - SPINE - TERMINAL - {"gate"} - EVOLVE_FROZEN


def insertable() -> frozenset[str]:
    """Nós que um operador estrutural pode inserir/remover."""
    return INSERTABLE


def EVENTS_ONLY_SAFE(spec: Mapping[str, Any] | None = None) -> frozenset[str]:
    """Nós que podem rodar em paralelo: os que só escrevem `events`.

    Fonte é o CONTRATO, não a leitura do código: nó de plugin passa pelo
    `plugin_nodes._wrap`, que descarta toda chave que não seja `events` — logo
    dois deles no mesmo super-step só disputam a chave que tem reducer. Nó
    builtin fica fora mesmo quando parece inofensivo: `reflect` devolve
    `reflect_hint`, que não tem reducer, e é exatamente a armadilha.

    Com `spec`, restringe aos nós declarados nela (é o uso do `check`); sem
    `spec`, é o registro inteiro (é o uso do proponente, que insere nó novo).
    """
    safe = plugin_nodes.registered() & frozenset(topology.NODE_IMPLS)
    if spec is None:
        return safe
    return safe & frozenset(spec.get("nodes") or ())


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
        if (
            src in SPINE_RANK
            and dst in SPINE_RANK
            and SPINE_RANK[src] > SPINE_RANK[dst]
            and (src, dst) not in BACK_EDGES
        ):
            reasons.append(f"{src}->{dst}: rank invertido")

    # (c) nó declarado e inalcançável é peso morto que ninguém revisou.
    orphans = sorted(set(nodes) - _reachable(nodes, pairs))
    if orphans:
        reasons.append(f"inalcançável de START: {orphans}")

    # (d) uma saída só, e por `record`: o ledger não pode ser contornado.
    to_end = [src for src, dst in pairs if dst == topology.END_NAME]
    if to_end != ["record"]:
        reasons.append(f"saída pro END tem que ser só record->END (achado: {to_end})")

    # (f) fan-out só como diamante fechado de nós events-only. Ramo que escreve
    # outra chave do RunState mata o run em runtime, e ramo que não converge no
    # mesmo merge deixa o grafo com dois caminhos independentes — nenhum dos
    # dois é "estrutura nova", os dois são bug. START fica fora: a entrada já é
    # regra (a). Terminal do gate não conta como ramo: as saídas do gate são
    # condicionais (uma por run), não paralelas.
    safe = EVENTS_ONLY_SAFE(spec)
    outgoing: dict[str, list[str]] = {}
    for src, dst in pairs:
        if src != topology.START_NAME:
            outgoing.setdefault(src, []).append(dst)
    for src, dsts in outgoing.items():
        branches = sorted({d for d in dsts if d not in TERMINAL and d != topology.END_NAME})
        if len(branches) < 2:
            continue
        unsafe = [b for b in branches if b not in safe]
        if unsafe:
            reasons.append(f"{src}->{branches}: fan-out com nó fora de events-only: {unsafe}")
        outs = {b: [d for s, d in pairs if s == b] for b in branches}
        loose = sorted(b for b, o in outs.items() if len(o) != 1)
        if loose:
            reasons.append(f"{src}->{branches}: ramo sem saída única: {loose}")
        merges = sorted({o[0] for o in outs.values() if len(o) == 1})
        if len(merges) > 1:
            reasons.append(f"{src}->{branches}: ramos não convergem: {merges}")

    return reasons
