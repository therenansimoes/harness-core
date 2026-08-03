"""Adaptadores de ação (actions.py) + plug do meta_check no autopilot.

Tudo em sandbox de tmp (config real do repo copiada), zero LLM, zero rede.
O plug do meta é testado chamando o nó `_apply` direto: o que se julga é a
decisão do nó (escala vs aplica), não o ciclo inteiro — o ciclo já tem dono
em test_autopilot.py.
"""

import shutil
import tomllib
from pathlib import Path

import pytest

from harness.graph import autopilot_graph as ag
from harness.graph import topology
from harness.graph.run_graph import CFG_DATA_DIR
from harness.graph.state import Budget
from harness.improve import actions as adapters
from harness.improve import mutate
from harness.improve.target import actions
from harness.ledger import store
from harness.types import RunRow

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
ECHO_UNIT = Path(__file__).parent / "fixtures" / "echo" / "unit.toml"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def run_row(unit_id: str, ok: bool = False, reason: str = "verify_failed") -> RunRow:
    return RunRow(
        run_id=f"r-{unit_id}-{reason}", unit_id=unit_id, project=None,
        backend="mock", model=None, tier="t0", kind="code", ok=ok,
        exit_reason=reason, sec_total=10.0, sec_provision=0.0, cost_usd=0.0,
        intervention=False, created_at=store.now_iso(),
    )


# --- registro -------------------------------------------------------------------


def test_acoes_registradas():
    found = actions()
    for name in ("research", "codegen", "synthesize", "topology", "evolve"):
        assert name in found, name
        assert found[name].name == name


def test_registro_sobrevive_modulo_opcional_ausente():
    # skills.attribution / prompt_evolve podem não existir (paralelo): o
    # registry não pode quebrar por isso.
    assert isinstance(actions(), dict)


# --- synthesize -----------------------------------------------------------------


def test_synthesize_propose_lista_falhas_e_apply_gera_quarentena(sandbox):
    src = sandbox / "benchmarks" / "held_in" / "echo"
    src.mkdir(parents=True)
    shutil.copy(ECHO_UNIT, src / "unit.toml")

    history = [run_row("echo"), run_row("passou", ok=True, reason="done")]
    proposal = adapters.propose_synthesize(history)
    assert proposal is not None
    assert proposal.unit_ids == ("echo",)

    created = adapters.apply_synthesize(proposal, root=sandbox)
    assert len(created) == 1
    exam = sandbox / "benchmarks" / "quarantine" / "echo" / "unit.toml"
    assert exam.is_file()
    assert tomllib.loads(exam.read_text(encoding="utf-8"))["id"] == "echo"


def test_synthesize_sem_falha_devolve_none():
    assert adapters.propose_synthesize([run_row("ok", ok=True, reason="done")]) is None


# --- topology -------------------------------------------------------------------


def test_topology_propose_insere_reflect_e_apply_compila(sandbox):
    proposal = adapters.propose_topology(root=sandbox)
    assert proposal is not None
    path = adapters.apply_topology(proposal, root=sandbox)
    spec = topology.load_spec(path)
    assert "reflect" in spec["nodes"]
    topology.compile_spec(spec)  # spec aplicada tem que compilar de verdade
    # variação já feita: não há segunda proposta igual
    assert adapters.propose_topology(root=sandbox) is None


def test_topology_invalida_recusa_sem_escrever(sandbox):
    before = (sandbox / "config" / "topology.toml").read_bytes()
    torta = adapters.TopologyProposal(
        target_file="config/topology.toml",
        new_text='nodes = ["plan"]\n\nedges = [["START", "plan"]]\n',
    )
    with pytest.raises(topology.TopologyError):
        adapters.apply_topology(torta, root=sandbox)
    assert (sandbox / "config" / "topology.toml").read_bytes() == before


# --- evolve ---------------------------------------------------------------------


def test_evolve_deterministico_com_seed_do_state(sandbox):
    state = {"thread_id": "t-abc", "cycle": 3}
    p1 = adapters.propose_evolve(state, root=sandbox)
    p2 = adapters.propose_evolve(state, root=sandbox)
    assert p1.seed == p2.seed
    assert p1.candidate == p2.candidate  # mesmo state => mesma proposta
    p3 = adapters.propose_evolve({"thread_id": "t-abc", "cycle": 4}, root=sandbox)
    assert p3.seed != p1.seed


def test_evolve_apply_escreve_candidato_via_genome(sandbox):
    proposal = adapters.propose_evolve({"thread_id": "t", "cycle": 1}, root=sandbox)
    assert proposal.target_file == "config/models.toml"
    path = adapters.apply_evolve(proposal, root=sandbox)
    assert tomllib.loads(path.read_text(encoding="utf-8")) == proposal.candidate


def test_evolve_alvo_imutavel_recusa(sandbox):
    proposal = adapters.EvolveProposal(
        target_file="benchmarks/sealed/exam.toml", candidate={"x": 1}, seed=0
    )
    with pytest.raises(mutate.GenomeViolation):
        adapters.apply_evolve(proposal, root=sandbox)
    assert not (sandbox / "benchmarks" / "sealed" / "exam.toml").exists()


# --- plug do meta_check no _apply do autopilot -----------------------------------

CATALOG_META = """
[improve]
n_per_arm = 6

[[rule]]
id = "afrouxa_regua"
target_file = "config/ruler.toml"
key = "gate.kpi_regression_tolerance"
from = 0.0
to = 0.1
fails_on = ["verify_failed"]
hypothesis = "sintética: mexe na régua — exige meta-exame"

[[rule]]
id = "floor_up"
target_file = "config/models.toml"
key = "router.prior_floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: knob comum"
"""


def apply_state(rule_id: str) -> dict:
    return {
        "cycle": 1, "cycles": 1, "units": ["echo"],
        "target": {"rule_id": rule_id}, "mutation": None,
        "budget": Budget(),
    }


def apply_config(sandbox: Path, **extra) -> dict:
    return {
        "configurable": {
            "thread_id": "t-meta",
            CFG_DATA_DIR: str(sandbox / "data"),
            ag.CFG_ROOT: str(sandbox),
            **extra,
        }
    }


@pytest.fixture
def meta_sandbox(sandbox):
    (sandbox / "config" / "catalog.toml").write_text(CATALOG_META, encoding="utf-8")
    return sandbox


def test_meta_plug_ruler_vai_pra_escalate(meta_sandbox):
    before = (meta_sandbox / "config" / "ruler.toml").read_bytes()
    update = ag._apply(apply_state("afrouxa_regua"), apply_config(meta_sandbox))
    assert update.get("escalation"), update
    assert update["escalation"]["evidence"]["meta"] == "blocked"  # exame default: False
    assert "mutation" not in update
    assert (meta_sandbox / "config" / "ruler.toml").read_bytes() == before


def test_meta_plug_exame_ok_sem_ack_quarentena(meta_sandbox):
    before = (meta_sandbox / "config" / "ruler.toml").read_bytes()
    cfg = apply_config(meta_sandbox, **{ag.CFG_SEALED_EXAM: lambda: True})
    update = ag._apply(apply_state("afrouxa_regua"), cfg)
    assert update.get("escalation"), update
    assert update["escalation"]["evidence"]["meta"] == "quarantined"
    assert (meta_sandbox / "config" / "ruler.toml").read_bytes() == before


def test_meta_plug_alvo_comum_aplica(meta_sandbox):
    update = ag._apply(apply_state("floor_up"), apply_config(meta_sandbox))
    assert "escalation" not in update, update
    assert update["mutation"]["rule_id"] == "floor_up"
    alvo = meta_sandbox / "config" / "models.toml"
    assert mutate.read_value(alvo, "router.prior_floor") == 0.65
