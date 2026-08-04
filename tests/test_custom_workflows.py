"""Biblioteca de workflows nomeados + ação 'workflow'.

Contrato testado: (1) list/load dos seeds do repo; (2) run_workflow roda a
unidade pelo grafo do spec nomeado (deep => evento reflect; hotfix aceita sem
measure); (3) spec inválida => TopologyError sem escrita; (4) a ação respeita
o genoma fail-closed e está no registry. O genoma aqui é INJETADO com
`config/workflows/*.toml` mutável — o genome.toml do repo ganha o padrão em
paralelo, este teste não depende dele.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.genome.genome import Genome
from harness.graph import custom, topology
from harness.improve import mutate, workflow_action

FIXTURE = Path(__file__).parent / "fixtures" / "echo"

VALID_SPEC = {
    "nodes": [
        "plan",
        "route",
        "provision",
        "execute",
        "verify",
        "measure",
        "gate",
        "accept",
        "retry",
        "escalate",
        "revert",
        "record",
    ],
    "edges": [
        ["START", "plan"],
        ["plan", "route"],
        ["route", "provision"],
        ["provision", "execute"],
        ["execute", "verify"],
        ["verify", "measure"],
        ["measure", "gate"],
        ["retry", "route"],
        ["accept", "record"],
        ["escalate", "record"],
        ["revert", "record"],
        ["record", "END"],
    ],
}

GENOME_ABERTO = Genome(immutable=("harness/**",), mutable=("config/workflows/*.toml",))
GENOME_FECHADO = Genome(immutable=("harness/**",), mutable=())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


# --- list/load -----------------------------------------------------------------


def test_list_workflows_do_repo():
    names = custom.list_workflows()
    assert "hotfix" in names
    assert "deep" in names


def test_list_workflows_dir_ausente_devolve_vazio(tmp_path):
    assert custom.list_workflows(tmp_path / "nao-existe") == []


def test_load_workflow_deep_tem_reflect():
    spec = custom.load_workflow("deep")
    assert "reflect" in spec["nodes"]


def test_load_workflow_inexistente_topology_error():
    with pytest.raises(topology.TopologyError, match="desconhecido"):
        custom.load_workflow("nao-existe")


def test_load_workflow_invalido_fail_closed(tmp_path):
    (tmp_path / "torto.toml").write_text('nodes = ["plan"]\nedges = []\n', encoding="utf-8")
    with pytest.raises(topology.TopologyError):
        custom.load_workflow("torto", dir=tmp_path)


# --- run_workflow --------------------------------------------------------------


def test_run_workflow_deep_gera_evento_reflect(data_dir):
    final = custom.run_workflow(
        "deep", FIXTURE, backend="mock", data_dir=data_dir, thread_id="t-wf-deep"
    )
    nodes = [e["node"] for e in final["events"]]
    assert final["decision"].action == "accept"
    assert "reflect" in nodes
    assert nodes.index("plan") < nodes.index("reflect") < nodes.index("route")


def test_run_workflow_hotfix_aceita_sem_measure(data_dir):
    final = custom.run_workflow(
        "hotfix", FIXTURE, backend="mock", data_dir=data_dir, thread_id="t-wf-hot"
    )
    assert final["decision"].action == "accept"
    assert "measure" not in [e["node"] for e in final["events"]]


# --- ação 'workflow' -----------------------------------------------------------


def test_propose_spec_invalida_recusa_sem_escrever():
    with pytest.raises(topology.TopologyError, match="obrigatórios"):
        workflow_action.propose_workflow(
            "quebrado", spec={"nodes": ["plan"], "edges": [["START", "plan"]]}
        )


def test_propose_nome_invalido_recusa():
    with pytest.raises(workflow_action.WorkflowActionError, match="inválido"):
        workflow_action.propose_workflow("../fuga", spec=VALID_SPEC)


def test_apply_fora_da_zona_mutavel_recusa_sem_escrever(tmp_path):
    prop = workflow_action.propose_workflow("novo", spec=VALID_SPEC)
    with pytest.raises(mutate.GenomeViolation):
        workflow_action.apply_workflow(prop, root=tmp_path, genome=GENOME_FECHADO)
    assert not (tmp_path / "config" / "workflows" / "novo.toml").exists()


def test_apply_na_zona_escreve_workflow_carregavel(tmp_path):
    prop = workflow_action.propose_workflow("novo", spec=VALID_SPEC)
    rec = workflow_action.apply_workflow(prop, root=tmp_path, genome=GENOME_ABERTO)
    path = tmp_path / "config" / "workflows" / "novo.toml"
    assert path.is_file()
    assert rec.workflow_path == "config/workflows/novo.toml"
    spec = custom.load_workflow("novo", dir=path.parent)
    assert spec["nodes"] == VALID_SPEC["nodes"]


def test_acao_registrada_no_registry():
    from harness.improve.target import actions

    acts = actions()
    assert "workflow" in acts
    assert acts["workflow"].propose is workflow_action.propose_workflow
