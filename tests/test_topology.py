"""Topologia declarável (config/topology.toml) — estilo ADAS.

O contrato testado: (1) o toml default do repo reproduz a topologia embutida;
(2) toml torto => fallback silencioso para a embutida; (3) a estrutura pode
crescer (nó `reflect` inserido por dado, sem tocar código de nó); (4) spec sem
nó obrigatório é recusada fail-closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.graph import topology
from harness.graph.run_graph import run_unit
from harness.routing import CONFIG_DIR_ENV

FIXTURE = Path(__file__).parent / "fixtures" / "echo"

BUILTIN_HAPPY_PATH = [
    "plan", "route", "provision", "execute", "verify",
    "measure", "gate", "accept", "record",
]

ALL_NODES = [
    "plan", "route", "provision", "execute", "verify", "measure",
    "gate", "accept", "retry", "escalate", "revert", "record",
]

DEFAULT_EDGES = [
    ["START", "plan"], ["plan", "route"], ["route", "provision"],
    ["provision", "execute"], ["execute", "verify"], ["verify", "measure"],
    ["measure", "gate"], ["retry", "route"], ["accept", "record"],
    ["escalate", "record"], ["revert", "record"], ["record", "END"],
]

# reflect entre plan e route: mesmo caminho feliz, com um nó a mais.
REFLECT_TOML = """\
nodes = ["plan","reflect","route","provision","execute","verify","measure","gate","accept","retry","escalate","revert","record"]
edges = [
  ["START","plan"], ["plan","reflect"], ["reflect","route"],
  ["route","provision"], ["provision","execute"], ["execute","verify"],
  ["verify","measure"], ["measure","gate"], ["retry","route"],
  ["accept","record"], ["escalate","record"], ["revert","record"],
  ["record","END"],
]
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _cfg_with(tmp_path, monkeypatch, topo_text: str) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "topology.toml").write_text(topo_text, encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def test_toml_default_igual_topologia_embutida(data_dir, capfd):
    # Sem HARNESS_CONFIG_DIR o config/topology.toml do repo é o carregado.
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-topo-default")
    assert final["decision"].action == "accept"
    assert [e["node"] for e in final["events"]] == BUILTIN_HAPPY_PATH
    # Sem linha de fallback => o toml do repo compilou de verdade.
    assert "topologia embutida" not in capfd.readouterr().err


def test_toml_malformado_cai_na_embutida(data_dir, tmp_path, monkeypatch, capfd):
    _cfg_with(tmp_path, monkeypatch, 'nodes = ["plan"  # toml torto\n')
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-topo-torto")
    assert final["decision"].action == "accept"
    assert [e["node"] for e in final["events"]] == BUILTIN_HAPPY_PATH
    assert "topologia embutida" in capfd.readouterr().err


def test_reflect_inserido_roda_e_gera_evento(data_dir, tmp_path, monkeypatch, capfd):
    _cfg_with(tmp_path, monkeypatch, REFLECT_TOML)
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-topo-reflect")
    nodes = [e["node"] for e in final["events"]]
    assert final["decision"].action == "accept"
    assert nodes == ["plan", "reflect"] + BUILTIN_HAPPY_PATH[1:]
    assert "topologia embutida" not in capfd.readouterr().err


def test_spec_sem_gate_recusada():
    spec = {
        "nodes": [n for n in ALL_NODES if n != "gate"],
        "edges": [e for e in DEFAULT_EDGES if e != ["measure", "gate"]],
    }
    with pytest.raises(topology.TopologyError, match="obrigatórios"):
        topology.compile_spec(spec)


def test_spec_com_impl_desconhecida_recusada():
    spec = {"nodes": ALL_NODES + ["hackear"], "edges": DEFAULT_EDGES}
    with pytest.raises(topology.TopologyError, match="desconhecida"):
        topology.compile_spec(spec)


def test_aresta_linear_saindo_do_gate_recusada():
    spec = {"nodes": ALL_NODES, "edges": DEFAULT_EDGES + [["gate", "record"]]}
    with pytest.raises(topology.TopologyError, match="gate"):
        topology.compile_spec(spec)
