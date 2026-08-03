"""Topologia por kind (harness/graph/by_kind.py).

O contrato testado: (1) retrocompat bit a bit — sem seção [kinds.*] todo kind
resolve para a topologia de topo, a mesma de sempre; (2) seção declarada vale
só para o seu kind, sem vazar para os outros; (3) seção parcial é recusada
fail-closed (nunca merge com o default); (4) build_for_unit é fail-open: toml
torto ainda devolve grafo, com 1 linha no stderr.
"""

from __future__ import annotations

import pytest

from harness.graph import by_kind, topology
from harness.routing import CONFIG_DIR_ENV
from harness.routing.kinds import VALID_KINDS
from harness.types import UnitSpec

# Topo = default (com reflect); [kinds.content] = grafo distinto (sem reflect,
# route direto no execute). Arrays de topo antes da tabela, como manda o TOML.
BY_KIND_TOML = """\
nodes = ["plan","reflect","route","provision","execute","verify","measure","gate","accept","retry","escalate","revert","record"]
edges = [
  ["START","plan"], ["plan","reflect"], ["reflect","route"],
  ["route","provision"], ["provision","execute"], ["execute","verify"],
  ["verify","measure"], ["measure","gate"], ["retry","route"],
  ["accept","record"], ["escalate","record"], ["revert","record"],
  ["record","END"],
]

[kinds.content]
nodes = ["plan","route","execute","verify","measure","gate","accept","retry","escalate","revert","record"]
edges = [
  ["START","plan"], ["plan","route"], ["route","execute"],
  ["execute","verify"], ["verify","measure"], ["measure","gate"],
  ["retry","route"], ["accept","record"], ["escalate","record"],
  ["revert","record"], ["record","END"],
]
"""

PARCIAL_TOML = """\
nodes = ["plan","route","provision","execute","verify","measure","gate","accept","retry","escalate","revert","record"]
edges = [
  ["START","plan"], ["plan","route"], ["route","provision"],
  ["provision","execute"], ["execute","verify"], ["verify","measure"],
  ["measure","gate"], ["retry","route"], ["accept","record"],
  ["escalate","record"], ["revert","record"], ["record","END"],
]

[kinds.code]
nodes = ["plan","route","provision","execute","verify","measure","gate","accept","retry","escalate","revert","record"]
"""


def _cfg_with(tmp_path, monkeypatch, topo_text: str):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "topology.toml").write_text(topo_text, encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def test_retrocompat_todo_kind_resolve_no_topo_do_repo():
    # config/topology.toml do repo não tem [kinds.*]: todo kind (e None) tem
    # que devolver a topologia de topo, bit a bit, e compilar.
    spec = topology.load_spec()
    esperado = {"nodes": spec["nodes"], "edges": spec["edges"]}
    assert by_kind.kinds_declared(spec) == []
    for kind in sorted(VALID_KINDS) + [None]:
        assert by_kind.resolve_spec(spec, kind) == esperado
        assert topology.compile_spec(by_kind.resolve_spec(spec, kind)) is not None


def test_secao_do_kind_nao_vaza_para_outro(tmp_path, monkeypatch):
    _cfg_with(tmp_path, monkeypatch, BY_KIND_TOML)
    spec = topology.load_spec()
    assert by_kind.kinds_declared(spec) == ["content"]

    content = by_kind.load_for_kind("content")
    assert "provision" not in content["nodes"]
    assert ["route", "execute"] in content["edges"]

    for outro in ("code", "refactor", None):
        assert by_kind.load_for_kind(outro) == {
            "nodes": spec["nodes"],
            "edges": spec["edges"],
        }
    # Sem merge: o default segue com reflect, a seção segue sem provision.
    assert "reflect" in by_kind.load_for_kind("code")["nodes"]
    assert "reflect" not in content["nodes"]
    assert topology.compile_spec(content) is not None


def test_kind_declarado_fora_de_valid_kinds_e_ignorado(tmp_path, monkeypatch):
    _cfg_with(tmp_path, monkeypatch, BY_KIND_TOML.replace("[kinds.content]", "[kinds.poesia]"))
    spec = topology.load_spec()
    assert by_kind.kinds_declared(spec) == ["poesia"]
    assert by_kind.load_for_kind("content") == {
        "nodes": spec["nodes"],
        "edges": spec["edges"],
    }


def test_kinds_vazio_cai_no_default(tmp_path, monkeypatch):
    _cfg_with(tmp_path, monkeypatch, PARCIAL_TOML.replace("[kinds.code]\nnodes = ", "# nodes = "))
    spec = topology.load_spec()
    assert by_kind.kinds_declared(spec) == []
    assert by_kind.load_for_kind("code")["nodes"] == spec["nodes"]


def test_secao_parcial_recusada(tmp_path, monkeypatch):
    _cfg_with(tmp_path, monkeypatch, PARCIAL_TOML)
    with pytest.raises(topology.TopologyError, match="parcial"):
        by_kind.load_for_kind("code")
    # Outro kind não é contaminado pela seção torta.
    assert by_kind.load_for_kind("content")["nodes"]


def test_toml_sem_nodes_de_topo_e_topology_error(tmp_path, monkeypatch):
    _cfg_with(tmp_path, monkeypatch, 'edges = [["START","plan"]]\n')
    with pytest.raises(topology.TopologyError):
        topology.compile_spec(by_kind.load_for_kind("code"))


def test_build_for_unit_fail_open_com_toml_torto(tmp_path, monkeypatch, capfd):
    _cfg_with(tmp_path, monkeypatch, 'nodes = ["plan"  # toml torto\n')
    unit = UnitSpec(id="u1", path=tmp_path, prompt="oi", verify_cmd="true", kind="content")
    graph = by_kind.build_for_unit(unit, None)
    assert graph is not None
    err = capfd.readouterr().err
    assert "by_kind" in err and "topologia default" in err


def test_build_for_unit_usa_secao_do_kind(tmp_path, monkeypatch, capfd):
    _cfg_with(tmp_path, monkeypatch, BY_KIND_TOML)
    unit = UnitSpec(id="u1", path=tmp_path, prompt="oi", verify_cmd="true", kind="content")
    assert by_kind.build_for_unit(unit, None) is not None
    assert "by_kind" not in capfd.readouterr().err
