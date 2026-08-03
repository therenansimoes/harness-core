"""policy.py: bandit de seleção de ação + plugs no autopilot.

Bandit puro é testado com histórico sintético (dicts — `policy._get` aceita
linha parcial). Os plugs rodam o autopilot inteiro em sandbox com backend mock
(mesmo padrão de test_autopilot.py) e chamam `_apply` direto para o default
lazy do exame selado.
"""

import random
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.improve as improve_pkg
from harness.graph import autopilot_graph as ag
from harness.graph.run_graph import CFG_DATA_DIR
from harness.graph.state import Budget
from harness.improve import mutate, policy
from harness.improve.target import actions
from harness.ledger import store
from harness.types import RunRow

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
UNIT = Path(__file__).parent / "fixtures" / "echo"

CATALOG = """
[improve]
sec_cost_usd = 0.0001
n_per_arm = 6

[[rule]]
id = "floor_up"
target_file = "config/models.toml"
key = "router.prior_floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: piso mais alto escala mais cedo"
"""

CATALOG_RULER = """
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
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "config" / "catalog.toml").write_text(CATALOG, encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def db(sandbox: Path) -> Path:
    return sandbox / "data" / store.DB_NAME


def seed_failures(sandbox: Path, n: int = 3) -> None:
    for i in range(n):
        store.record_run(
            RunRow(
                run_id=f"seed{i}", unit_id="echo", project=None, backend="mock",
                model=None, tier="t0", kind="code", ok=False,
                exit_reason="verify_failed", sec_total=10.0, sec_provision=0.0,
                cost_usd=0.0, intervention=False, created_at=store.now_iso(),
            ),
            path=db(sandbox),
        )


def rows(
    name: str,
    keep: int,
    other: int,
    other_verdict: str = "DISCARD",
    kind: str | None = None,
) -> list[dict]:
    tag = policy.note_with_action(name, None, kind=kind)
    return (
        [{"verdict": "KEEP", "note": tag}] * keep
        + [{"verdict": other_verdict, "note": tag}] * other
    )


# --- bandit puro ----------------------------------------------------------------


def test_prefere_keep_rate_alto_com_amostra():
    history = rows("boa", 9, 1) + rows("ruim", 1, 9)
    got = policy.select_action(["boa", "ruim"], history, random.Random(0))
    assert got == "boa"


def test_explora_acao_sem_amostra():
    # "nova" nunca rodou: bônus infinito ganha de qualquer veterana.
    history = rows("boa", 9, 1) + rows("ruim", 1, 9)
    got = policy.select_action(["boa", "ruim", "nova"], history, random.Random(0))
    assert got == "nova"


def test_deterministico_com_rng_seedado():
    # Empate (duas sem amostra): o desempate é do rng, e rng igual → escolha igual.
    history = rows("boa", 9, 1)
    picks = {
        policy.select_action(["boa", "x", "y"], history, random.Random(42))
        for _ in range(10)
    }
    assert len(picks) == 1
    assert picks.pop() in {"x", "y"}


def test_sem_acoes_e_erro():
    with pytest.raises(ValueError):
        policy.select_action([], [], random.Random(0))


def test_action_stats_conta_so_veredito_concluido():
    history = (
        rows("a", 2, 1)
        + rows("a", 0, 1, other_verdict="INCONCLUSIVE")
        + [{"verdict": "ABORTED", "note": "action=a;error"}]   # não conta
        + [{"verdict": "KEEP", "note": None}]                   # sem token: fora
    )
    stats = policy.action_stats(history)
    assert set(stats) == {"a"}
    assert (stats["a"]["keep"], stats["a"]["n"]) == (2, 4)
    assert stats["a"]["rate"] == pytest.approx(0.5)
    assert 0.0 < stats["a"]["lower"] < 0.5


# --- prior por (kind, ação) -----------------------------------------------------


def test_prior_por_kind_escolhe_diferente_por_tipo():
    """`a` paga em code, `b` paga em content: o global empata, a célula decide."""
    history = (
        rows("a", 8, 1, kind="code") + rows("b", 1, 8, kind="code")
        + rows("a", 1, 8, kind="content") + rows("b", 8, 1, kind="content")
    )
    # Global: 9/18 para as duas — sem kind o bandit não tem como diferenciar.
    glob = policy.action_stats(history)
    assert glob["a"]["lower"] == pytest.approx(glob["b"]["lower"])

    assert policy.select_action(["a", "b"], history, random.Random(0), kind="code") == "a"
    assert (
        policy.select_action(["a", "b"], history, random.Random(0), kind="content") == "b"
    )


def test_celula_rala_nao_vira_o_jogo():
    """1 amostra na célula pesa 1/5: o agregado global continua mandando."""
    history = (
        rows("boa", 17, 2) + rows("ruim", 2, 17)
        + rows("boa", 0, 1, kind="code")      # única amostra em code: DISCARD
        + rows("ruim", 1, 0, kind="code")     # única amostra em code: KEEP
    )
    cell = policy.action_stats(history, kind="code")
    assert (cell["boa"]["n"], cell["ruim"]["n"]) == (1, 1)
    assert policy.select_action(["boa", "ruim"], history, random.Random(0), kind="code") == "boa"


def test_celula_vazia_usa_global_e_acao_virgem_ainda_explora():
    """Kind sem nenhuma amostra: vale o global (não zera, não vira inf)."""
    history = rows("boa", 9, 1, kind="code") + rows("ruim", 1, 9, kind="code")
    assert (
        policy.select_action(["boa", "ruim"], history, random.Random(0), kind="content")
        == "boa"
    )
    # "nova" não tem amostra em lugar nenhum: exploração como sempre.
    assert (
        policy.select_action(
            ["boa", "ruim", "nova"], history, random.Random(0), kind="content"
        )
        == "nova"
    )


def test_note_kind_roundtrip():
    assert policy.note_with_action("research", None, kind="code") == "action=research;kind=code"
    assert (
        policy.note_with_action("research", "deadline", kind="code")
        == "action=research;kind=code;deadline"
    )
    assert policy.note_with_action(None, "deadline", kind="code") == "deadline"
    linha = SimpleNamespace(note="action=research;kind=code;deadline", verdict="KEEP")
    assert (policy.action_of(linha), policy.kind_of(linha)) == ("research", "code")
    assert policy.kind_of(SimpleNamespace(note="action=research")) is None


def test_note_roundtrip():
    assert policy.note_with_action("research", None) == "action=research"
    assert policy.note_with_action("research", "deadline") == "action=research;deadline"
    assert policy.note_with_action(None, "deadline") == "deadline"
    linha = SimpleNamespace(note="action=research;deadline", verdict="KEEP")
    assert policy.action_of(linha) == "research"
    assert policy.action_of(SimpleNamespace(note="deadline")) is None


# --- autopilot registra a ação --------------------------------------------------


def test_autopilot_registra_acao_fixada(sandbox):
    """`action=` do chamador vai parar no note da mutação e no result."""
    seed_failures(sandbox)
    report = ag.run_autopilot(
        sandbox / "data", units=[UNIT], root=sandbox,
        backend="mock", action="research",
    )
    r = report.results[0]
    assert r["verdict"] == "INCONCLUSIVE"   # mock empata os braços
    assert r["action"] == "research"
    linha = store.mutations(path=db(sandbox))[0]
    assert policy.action_of(linha) == "research"


def test_autopilot_policy_escolhe_e_registra(sandbox):
    """Sem ação fixada, a policy escolhe uma do registry e ela fica no ledger."""
    seed_failures(sandbox)
    report = ag.run_autopilot(
        sandbox / "data", units=[UNIT], root=sandbox, backend="mock",
    )
    r = report.results[0]
    assert r["action"] in actions()
    assert policy.action_of(store.mutations(path=db(sandbox))[0]) == r["action"]


# --- default lazy do exame selado -----------------------------------------------


def apply_state(rule_id: str) -> dict:
    return {
        "cycle": 1, "cycles": 1, "units": ["echo"],
        "target": {"rule_id": rule_id}, "mutation": None,
        "budget": Budget(),
    }


def apply_config(sandbox: Path, **extra) -> dict:
    return {
        "configurable": {
            "thread_id": "t-policy",
            CFG_DATA_DIR: str(sandbox / "data"),
            ag.CFG_ROOT: str(sandbox),
            **extra,
        }
    }


@pytest.fixture
def ruler_sandbox(sandbox):
    (sandbox / "config" / "catalog.toml").write_text(CATALOG_RULER, encoding="utf-8")
    return sandbox


def _fake_exam(result: bool, calls: list):
    def run_sealed_exam(**kw):
        calls.append(kw)
        return result

    return SimpleNamespace(
        SEALED_DIR=Path("benchmarks/sealed"), run_sealed_exam=run_sealed_exam
    )


def test_default_exame_usa_exam_real_guardado(ruler_sandbox, monkeypatch):
    """Sem CFG_SEALED_EXAM no config, o `_apply` consulta exam.run_sealed_exam.

    Fake devolvendo True: o veredito meta vira "quarantined" (exame ok, sem
    ack) em vez do "blocked" do antigo `lambda: False` — prova que o default
    passou pelo exame, amarrado à raiz do ciclo.
    """
    calls: list = []
    fake = _fake_exam(True, calls)
    monkeypatch.setattr(improve_pkg, "exam", fake, raising=False)
    monkeypatch.setitem(sys.modules, "harness.improve.exam", fake)

    update = ag._apply(apply_state("afrouxa_regua"), apply_config(ruler_sandbox))

    assert update["escalation"]["evidence"]["meta"] == "quarantined"
    assert len(calls) == 1
    assert calls[0]["sealed_dir"] == ruler_sandbox / "benchmarks" / "sealed"
    # nada aplicado: a régua está intacta
    assert mutate.read_value(
        ruler_sandbox / "config" / "ruler.toml", "gate.kpi_regression_tolerance"
    ) == 0.0


def test_default_exame_modulo_ausente_fail_closed(ruler_sandbox, monkeypatch):
    """exam.py ausente (ImportError): default degrada para `lambda: False`."""
    monkeypatch.delattr(improve_pkg, "exam", raising=False)
    monkeypatch.setitem(sys.modules, "harness.improve.exam", None)

    fn = ag._default_sealed_exam(apply_config(ruler_sandbox))
    assert fn() is False

    update = ag._apply(apply_state("afrouxa_regua"), apply_config(ruler_sandbox))
    assert update["escalation"]["evidence"]["meta"] == "blocked"


def test_exame_explicito_injetado_inalterado(ruler_sandbox, monkeypatch):
    """CFG_SEALED_EXAM no config curto-circuita o default: fake nunca é chamado."""
    calls: list = []
    fake = _fake_exam(True, calls)
    monkeypatch.setattr(improve_pkg, "exam", fake, raising=False)
    monkeypatch.setitem(sys.modules, "harness.improve.exam", fake)

    cfg = apply_config(ruler_sandbox, **{ag.CFG_SEALED_EXAM: lambda: False})
    update = ag._apply(apply_state("afrouxa_regua"), cfg)

    assert update["escalation"]["evidence"]["meta"] == "blocked"
    assert calls == []
