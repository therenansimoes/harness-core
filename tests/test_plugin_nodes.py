"""Whitelist que cresce: `plugins/nodes/*.py` entrando em topology.NODE_IMPLS.

O contrato testado é a tranca dupla: (1) módulo válido COM aprovação de sha256
registra, e uma spec que cita o nó compila; (2) cada recusa isolada — sem
aprovação, hash divergente, AST torto, import que explode, import proibido, nome
inválido — não registra e não levanta; (3) plugin não sombreia builtin, retorno
com chave extra é filtrado para só `events`, kill switch zera tudo.

`NODE_IMPLS` é dict global: a fixture restaura, senão um teste que registra `foo`
vaza para o resto da suíte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.genome.genome import Genome
from harness.graph import plugin_nodes, topology
from harness.improve import node_action

GENOME = Genome(immutable=("harness/**", "uv.lock"), mutable=("plugins/**",))

VALID = """\
from harness.ledger import store


def node(state, config=None) -> dict:
    return {"events": [{"node": "extra", "at": store.now_iso()}]}
"""

ALL_NODES = [
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
]

DEFAULT_EDGES = [
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
]


@pytest.fixture(autouse=True)
def restore_impls():
    """NODE_IMPLS e o cache de registrados voltam ao estado anterior."""
    before = dict(topology.NODE_IMPLS)
    registered = dict(plugin_nodes._REGISTERED)
    yield
    topology.NODE_IMPLS.clear()
    topology.NODE_IMPLS.update(before)
    plugin_nodes._REGISTERED.clear()
    plugin_nodes._REGISTERED.update(registered)


@pytest.fixture()
def root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(plugin_nodes.KILL_SWITCH, raising=False)
    monkeypatch.delenv(node_action.ACK_ENV, raising=False)
    (tmp_path / plugin_nodes.NODES_SUBDIR).mkdir(parents=True)
    return tmp_path


def write_node(root: Path, name: str, source: str = VALID) -> Path:
    path = root / plugin_nodes.NODES_SUBDIR / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def approve(root: Path, name: str, digest: str | None = None) -> None:
    path = root / plugin_nodes.NODES_SUBDIR / f"{name}.py"
    plugin_nodes.record_approval(
        name, digest if digest is not None else plugin_nodes.file_sha256(path)
    )


# --- 1. caminho aprovado --------------------------------------------------------


def test_modulo_aprovado_registra_e_spec_com_o_no_compila(root: Path) -> None:
    write_node(root, "extra")
    approve(root, "extra")

    assert plugin_nodes.register_all(root=root) == {"extra": "registered"}
    assert "extra" in topology.NODE_IMPLS

    spec = {
        "nodes": [*ALL_NODES, "extra"],
        "edges": [
            *[e for e in DEFAULT_EDGES if e != ["plan", "route"]],
            ["plan", "extra"],
            ["extra", "route"],
        ],
    }
    topology.compile_spec(spec)  # inválida => TopologyError


def test_registro_e_idempotente(root: Path) -> None:
    write_node(root, "extra")
    approve(root, "extra")

    primeiro = topology.NODE_IMPLS.copy()
    assert plugin_nodes.register_all(root=root) == {"extra": "registered"}
    impl = topology.NODE_IMPLS["extra"]
    assert plugin_nodes.register_all(root=root) == {"extra": "registered"}
    assert topology.NODE_IMPLS["extra"] is impl
    assert set(topology.NODE_IMPLS) - set(primeiro) == {"extra"}


def test_acao_node_com_ack_aprova_e_o_registry_carrega(root: Path) -> None:
    """Ponta a ponta: a ação escreve+aprova, o registry aceita o mesmo hash."""
    proposal = node_action.propose_node("greet", root=root, genome=GENOME)
    record = node_action.apply_node(
        proposal, root=root, genome=GENOME, run_exam=lambda: True, human_ack=True
    )

    assert record.verdict == "KEEP" and record.approved
    assert (root / proposal.target_file).is_file()
    assert plugin_nodes.register_all(root=root) == {"greet": "registered"}


def test_acao_node_sem_ack_descarta_e_nao_aprova(root: Path) -> None:
    """Exame passou, ack não veio: arquivo removido, nada aprovado."""
    proposal = node_action.propose_node("greet", root=root, genome=GENOME)
    record = node_action.apply_node(
        proposal, root=root, genome=GENOME, run_exam=lambda: True, human_ack=None
    )

    assert record.verdict == "DISCARD"
    assert not record.approved and record.sha256 is None
    assert not (root / proposal.target_file).exists()
    assert plugin_nodes.load_approvals() == {}
    assert "greet" not in topology.NODE_IMPLS


# --- 2. cada recusa, isolada ----------------------------------------------------

SEM_NODE = "def outro(state, config=None):\n    return {}\n"
AST_TORTO = "def node(state, config=None:\n"
IMPORT_EXPLODE = VALID + "\nraise RuntimeError('boom')\n"
IMPORT_PROIBIDO = "import subprocess\n\n\ndef node(state, config=None):\n    return {}\n"
EVAL = "def node(state, config=None):\n    return eval('{}')\n"
ASSINATURA = "def node(estado):\n    return {}\n"


@pytest.mark.parametrize(
    "name,source,aprovar,motivo",
    [
        ("semack", VALID, None, "sem aprovação"),
        ("divergente", VALID, "0" * 64, "hash divergente"),
        ("torto", AST_TORTO, "auto", "sintaxe inválida"),
        ("semnode", SEM_NODE, "auto", "não define node()"),
        ("explode", IMPORT_EXPLODE, "auto", "import falhou"),
        ("proibido", IMPORT_PROIBIDO, "auto", "import proibido"),
        ("comeval", EVAL, "auto", "nome proibido"),
        ("assinatura", ASSINATURA, "auto", "assinatura"),
        ("X", VALID, "auto", "nome inválido"),
        ("ab", VALID, "auto", "nome inválido"),
    ],
)
def test_recusa_nao_registra_e_nao_levanta(
    root: Path, name: str, source: str, aprovar: str | None, motivo: str
) -> None:
    write_node(root, name, source)
    if aprovar == "auto":
        approve(root, name)
    elif aprovar is not None:
        approve(root, name, aprovar)

    result = plugin_nodes.register_all(root=root)

    assert motivo in result[name], result[name]
    assert name not in topology.NODE_IMPLS


def test_arquivo_editado_depois_do_ack_perde_o_registro(root: Path) -> None:
    write_node(root, "extra")
    approve(root, "extra")
    write_node(root, "extra", VALID + "\nMEXIDO = 1\n")

    result = plugin_nodes.register_all(root=root)

    assert "hash divergente" in result["extra"]
    assert "extra" not in topology.NODE_IMPLS


# --- 3. limites: builtin, filtro do retorno, kill switch ------------------------


def test_execute_nao_sombreia_builtin(root: Path) -> None:
    antes = topology.NODE_IMPLS["execute"]
    write_node(root, "execute")
    approve(root, "execute")

    assert plugin_nodes.register_all(root=root) == {"execute": "builtin"}
    assert topology.NODE_IMPLS["execute"] is antes


def test_retorno_filtrado_para_so_events(root: Path, capsys) -> None:
    write_node(
        root,
        "extra",
        "def node(state, config=None):\n"
        "    return {'events': [{'node': 'extra'}], 'decision': 'accept', 'verdict': 'KEEP'}\n",
    )
    approve(root, "extra")
    plugin_nodes.register_all(root=root)

    out = topology.NODE_IMPLS["extra"]({}, None)

    assert out == {"events": [{"node": "extra"}]}
    assert "decision" in capsys.readouterr().err


def test_retorno_sem_events_e_nao_dict_sao_no_op(root: Path) -> None:
    write_node(root, "vazio", "def node(state, config=None):\n    return {'x': 1}\n")
    write_node(root, "naodict", "def node(state, config=None):\n    return 42\n")
    approve(root, "vazio")
    approve(root, "naodict")
    plugin_nodes.register_all(root=root)

    assert topology.NODE_IMPLS["vazio"]({}, None) == {}
    assert topology.NODE_IMPLS["naodict"]({}, None) == {}


@pytest.mark.parametrize("valor", ["0", "off", "false", "FALSE"])
def test_kill_switch_desliga_ate_no_aprovado(root: Path, monkeypatch, valor: str) -> None:
    write_node(root, "extra")
    approve(root, "extra")
    monkeypatch.setenv(plugin_nodes.KILL_SWITCH, valor)

    assert plugin_nodes.register_all(root=root) == {}
    assert "extra" not in topology.NODE_IMPLS
    assert plugin_nodes.disabled() is True


def test_dir_ausente_e_no_op(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))

    assert plugin_nodes.register_all(root=tmp_path) == {}
