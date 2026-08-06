"""Operadores estruturais da topologia por kind (topology_evolve + grammar).

Tudo em sandbox de tmp com a config REAL do repo copiada: o que se julga é o
comportamento sobre o default que está no disco, não sobre um toml de
laboratório. Zero LLM, zero rede. O fuzz é o teste central — o contrato do
módulo é "proposta legal ou None", e isso só se prova em volume.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from random import Random

import pytest

from harness.graph import by_kind, plugin_nodes, run_graph, topology
from harness.improve import actions as adapters
from harness.improve import topology_evolve as tev
from harness.improve import topology_grammar as gram

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
KINDS = ("code", "content", "config", "refactor", "infra")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def fake_plugins():
    """Dois nós de plugin já registrados. O repo real não tem plugin aprovado,
    então sem isso `split_parallel` é sempre None e o diamante nunca existe.
    Entram nos DOIS registros (whitelist do runtime + registro de plugin) e
    saem no finally: NODE_IMPLS é global e vazar daqui contamina outro teste."""
    names = ("probe_alpha", "probe_beta")

    def make(name):
        def _node(state, config=None) -> dict:
            return {"events": [run_graph._event(name)]}

        return _node

    for n in names:
        topology.NODE_IMPLS[n] = make(n)
        plugin_nodes._REGISTERED[n] = "fake"
    try:
        yield names
    finally:
        for n in names:
            topology.NODE_IMPLS.pop(n, None)
            plugin_nodes._REGISTERED.pop(n, None)


def topo_path(sandbox: Path) -> Path:
    return sandbox / "config" / "topology.toml"


def sem_reflect(sandbox: Path) -> Path:
    """Default anterior ao reflect/advise (retry->route direto): sem isso o
    `insert_node` não tem material, já que `reflect` é o único insertable
    (advise é EVOLVE_FROZEN — nunca candidato — e tirar só `reflect` deixaria
    `advise` órfão, sem aresta de entrada)."""
    p = topo_path(sandbox)
    spec = topology.load_spec(p)
    edges = [tuple(e) for e in spec["edges"] if "reflect" not in e and "advise" not in e]
    p.write_text(
        adapters.render_topology(
            {
                "nodes": [n for n in spec["nodes"] if n not in ("reflect", "advise")],
                "edges": [list(e) for e in [*edges, ("retry", "route")]],
            }
        ),
        encoding="utf-8",
    )
    return p


# --- gramática ------------------------------------------------------------------


def test_default_do_repo_e_legal():
    # A perna do retry via reflect (retry->reflect->route) é o default real: se
    # a gramática recusar isso, ela está errada, não o repo.
    assert gram.check(topology.load_spec()) == []


def test_insertable_nao_contem_espinha_nem_terminal():
    assert frozenset({"reflect"}) == gram.INSERTABLE
    assert not gram.INSERTABLE & (gram.SPINE | gram.TERMINAL)


def test_advise_nunca_e_candidato_de_mutacao(sandbox):
    """`advise` é nó real — está em NODE_IMPLS e no default do repo, com o
    mesmo perfil estrutural de `reflect` (grau 1/1 na perna do retry). Nó
    PAGO não é material de mutação estrutural: fica de fora de
    `gram.INSERTABLE` e nenhum operador (direto ou via `propose`, em nenhuma
    seed) produz proposta que insira, remova ou religue `advise`."""
    assert "advise" in topology.NODE_IMPLS
    assert "advise" in topology.load_spec()["nodes"]
    assert "advise" not in gram.INSERTABLE  # fora mesmo tendo o grau certo

    # candidato direto: grafo onde advise tem grau 1/1, igual a reflect.
    nodes = ["retry", "reflect", "advise", "route"]
    pairs = [("retry", "reflect"), ("reflect", "advise"), ("advise", "route")]
    vistos_remove = 0
    for seed in range(200):
        out = tev._remove_node(list(nodes), list(pairs), Random(seed))
        if out is None:
            continue
        vistos_remove += 1
        removed = set(nodes) - set(out[0])
        assert removed == {"reflect"}
    assert vistos_remove > 0

    bare_nodes = ["retry", "route"]
    bare_pairs = [("retry", "route")]
    vistos_insert = 0
    for seed in range(200):
        out = tev._insert_node(list(bare_nodes), list(bare_pairs), Random(seed))
        if out is None:
            continue
        vistos_insert += 1
        added = set(out[0]) - set(bare_nodes)
        assert added == {"reflect"}
    assert vistos_insert > 0

    # integração: fuzz de propose por cima do default real do repo.
    vistas = 0
    for seed in range(200):
        for operator in sorted(tev.OPERATORS):
            for kind in KINDS:
                p = tev.propose(kind, operator, Random(seed), root=sandbox)
                if p is None:
                    continue
                vistas += 1
                spec = tomllib.loads(p.new_text)
                section = by_kind.resolve_spec(spec, kind)
                pairs = tev._pairs(section)
                # advise nunca é o nó inserido/removido: continua presente e com
                # o mesmo grau 1/1 de sempre (vizinhos podem mudar — reflect
                # saindo religa retry->advise — mas advise em si não é tocado).
                assert "advise" in section["nodes"]
                assert tev._degrees(pairs, "advise") == (1, 1)
    assert vistas > 0


def test_aresta_rank_invertida_rejeitada():
    spec = topology.load_spec()
    torta = {
        "nodes": list(spec["nodes"]),
        "edges": [list(e) for e in spec["edges"]] + [["measure", "plan"]],
    }
    assert any("rank invertido" in r for r in gram.check(torta))


def test_saida_extra_pro_end_rejeitada():
    spec = topology.load_spec()
    torta = {
        "nodes": list(spec["nodes"]),
        "edges": [list(e) for e in spec["edges"]] + [["accept", "END"]],
    }
    assert any("END" in r for r in gram.check(torta))


def test_no_orfao_rejeitado():
    spec = topology.load_spec()
    torta = {
        "nodes": list(spec["nodes"]),
        "edges": [e for e in spec["edges"] if list(e) != ["retry", "reflect"]],
    }
    assert any("inalcançável" in r for r in gram.check(torta))


# --- operadores -----------------------------------------------------------------


@pytest.mark.parametrize("operator", sorted(tev.OPERATORS))
def test_cada_operador_no_toml_real_compila_ou_none(sandbox, operator):
    proposal = tev.propose("code", operator, Random(7), root=sandbox)
    if proposal is None:
        return
    spec = tomllib.loads(proposal.new_text)
    assert topology.compile_spec(by_kind.resolve_spec(spec, "code")) is not None


def test_remove_reflect_no_default_e_a_proposta_do_kind(sandbox):
    proposal = tev.propose("code", "remove_node", Random(1), root=sandbox)
    assert proposal is not None
    spec = tomllib.loads(proposal.new_text)
    # topo intocado, kind com corpo próprio sem reflect
    assert "reflect" in spec["nodes"]
    assert "reflect" not in spec["kinds"]["code"]["nodes"]
    # advise fica na espinha default (desarmado é grátis); tirar o reflect
    # reconecta retry→advise, não retry→route
    assert ["retry", "advise"] in spec["kinds"]["code"]["edges"]


def test_insert_node_materializa_secao_do_kind(sandbox):
    sem_reflect(sandbox)
    proposal = tev.propose("content", "insert_node", Random(3), root=sandbox)
    assert proposal is not None
    spec = tomllib.loads(proposal.new_text)
    assert "reflect" not in spec["nodes"]
    assert "reflect" in spec["kinds"]["content"]["nodes"]
    assert gram.check(by_kind.resolve_spec(spec, "content")) == []


def test_split_parallel_gera_diamante_legal_que_compila(sandbox, fake_plugins):
    proposal = tev.propose("code", "split_parallel", Random(5), root=sandbox)
    assert proposal is not None
    section = by_kind.resolve_spec(tomllib.loads(proposal.new_text), "code")
    assert gram.check(section) == []
    edges = [tuple(e) for e in section["edges"]]
    entradas = {s for s, d in edges if d in fake_plugins}
    saidas = {d for s, d in edges if s in fake_plugins}
    assert len(entradas) == 1 and len(saidas) == 1  # um src, um merge
    assert topology.compile_spec(section) is not None


def test_fan_out_ilegal_rejeitado(sandbox, fake_plugins):
    spec = topology.load_spec(topo_path(sandbox))
    base = [tuple(e) for e in spec["edges"] if list(e) != ["verify", "measure"]]
    n1, n2 = fake_plugins

    # ramo que escreve estado (measure) junto de um events-only
    sujo = {
        "nodes": [*list(spec["nodes"]), n1],
        "edges": [list(e) for e in [*base, ("verify", n1), ("verify", "measure"), (n1, "measure")]],
    }
    assert any("fora de events-only" in r for r in gram.check(sujo))

    # dois events-only, mas cada um num destino: diamante aberto
    aberto = {
        "nodes": [*list(spec["nodes"]), n1, n2],
        "edges": [
            list(e) for e in [*base, ("verify", n1), ("verify", n2), (n1, "measure"), (n2, "gate")]
        ],
    }
    assert any("não convergem" in r for r in gram.check(aberto))


def test_fuzz_split_parallel_e_none_ou_legal(sandbox, fake_plugins):
    vistas = 0
    for seed in range(200):
        for kind in KINDS:
            p = tev.propose(kind, "split_parallel", Random(seed), root=sandbox)
            if p is None:
                continue
            vistas += 1
            assert gram.check(by_kind.resolve_spec(tomllib.loads(p.new_text), kind)) == []
    assert vistas > 0


def test_operador_desconhecido_levanta(sandbox):
    with pytest.raises(KeyError):
        tev.propose("code", "explode", Random(0), root=sandbox)


def test_fuzz_proposta_e_none_ou_legal(sandbox):
    ilegais: list[tuple] = []
    vistas = 0
    for seed in range(200):
        for operator in sorted(tev.OPERATORS):
            for kind in KINDS:
                p = tev.propose(kind, operator, Random(seed), root=sandbox)
                if p is None:
                    continue
                vistas += 1
                spec = tomllib.loads(p.new_text)
                reasons = gram.check(by_kind.resolve_spec(spec, kind))
                if reasons:
                    ilegais.append((seed, operator, kind, reasons))
    assert not ilegais
    assert vistas > 0  # fuzz que só devolve None não testa nada


def test_fuzz_remove_nunca_tira_espinha_nem_terminal(sandbox):
    obrigatorios = gram.SPINE | gram.TERMINAL
    for seed in range(200):
        for kind in KINDS:
            p = tev.propose(kind, "remove_node", Random(seed), root=sandbox)
            if p is None:
                continue
            nodes = set(tomllib.loads(p.new_text)["kinds"][kind]["nodes"])
            assert obrigatorios <= nodes, (seed, kind, sorted(obrigatorios - nodes))


def test_fuzz_com_reflect_ausente_tambem_e_legal(sandbox):
    # Cobre o insert_node, que no default do repo não tem material.
    sem_reflect(sandbox)
    for seed in range(200):
        for operator in sorted(tev.OPERATORS):
            for kind in KINDS:
                p = tev.propose(kind, operator, Random(seed), root=sandbox)
                if p is None:
                    continue
                spec = tomllib.loads(p.new_text)
                assert gram.check(by_kind.resolve_spec(spec, kind)) == []


def test_propose_e_deterministico_e_nao_escreve(sandbox):
    antes = topo_path(sandbox).read_bytes()
    a = tev.propose("code", "remove_node", Random(11), root=sandbox)
    b = tev.propose("code", "remove_node", Random(11), root=sandbox)
    assert a == b
    assert topo_path(sandbox).read_bytes() == antes


# --- render / round-trip --------------------------------------------------------


def test_round_trip_preserva_topo_e_secoes(sandbox):
    spec = topology.load_spec(topo_path(sandbox))
    full = {
        **spec,
        "kinds": {
            "refactor": {"nodes": list(spec["nodes"]), "edges": [list(e) for e in spec["edges"]]},
            "code": {"nodes": list(spec["nodes"]), "edges": [list(e) for e in spec["edges"]]},
        },
    }
    back = tomllib.loads(tev.render(full))
    assert back == full
    assert list(back["kinds"]) == ["code", "refactor"]  # ordem estável


def test_apply_valida_todos_os_kinds_e_escreve(sandbox):
    proposal = tev.propose("code", "remove_node", Random(1), root=sandbox)
    path = tev.apply(proposal, root=sandbox)
    spec = topology.load_spec(path)
    assert "reflect" not in spec["kinds"]["code"]["nodes"]
    # os dois corpos compilam de verdade depois da escrita
    assert topology.compile_spec(by_kind.resolve_spec(spec, "code")) is not None
    assert topology.compile_spec(by_kind.resolve_spec(spec, None)) is not None


def test_apply_recusa_kind_torto_sem_escrever(sandbox):
    antes = topo_path(sandbox).read_bytes()
    spec = topology.load_spec(topo_path(sandbox))
    torta = adapters.TopologyProposal(
        target_file=adapters.TOPOLOGY_FILE,
        new_text=tev.render(
            {**spec, "kinds": {"code": {"nodes": ["plan"], "edges": [["START", "plan"]]}}}
        ),
    )
    with pytest.raises(topology.TopologyError):
        tev.apply(torta, root=sandbox)
    assert topo_path(sandbox).read_bytes() == antes


# --- bug do render legado -------------------------------------------------------


def test_render_topology_legado_preserva_kinds(sandbox):
    spec = topology.load_spec(topo_path(sandbox))
    full = {
        **spec,
        "kinds": {
            "code": {
                "nodes": [
                    "plan",
                    "execute",
                    "verify",
                    "gate",
                    "accept",
                    "retry",
                    "escalate",
                    "revert",
                    "record",
                ],
                "edges": [
                    ["START", "plan"],
                    ["plan", "execute"],
                    ["execute", "verify"],
                    ["verify", "gate"],
                    ["retry", "execute"],
                    ["accept", "record"],
                    ["escalate", "record"],
                    ["revert", "record"],
                    ["record", "END"],
                ],
            }
        },
    }
    back = tomllib.loads(adapters.render_topology(full))
    assert back["kinds"] == full["kinds"]
    assert back["nodes"] == full["nodes"]


def test_propose_topology_legado_nao_apaga_secao_de_kind(sandbox):
    p = sem_reflect(sandbox)
    spec = topology.load_spec(p)
    secao = {
        "nodes": list(spec["nodes"]),
        "edges": [list(e) for e in spec["edges"]],
    }
    p.write_text(adapters.render_topology({**spec, "kinds": {"infra": secao}}), encoding="utf-8")

    proposal = adapters.propose_topology(root=sandbox)
    assert proposal is not None
    assert tomllib.loads(proposal.new_text)["kinds"]["infra"] == secao
    path = adapters.apply_topology(proposal, root=sandbox)
    depois = topology.load_spec(path)
    assert depois["kinds"]["infra"] == secao  # apply não apagou a seção
    assert "reflect" in depois["nodes"]
