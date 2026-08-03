"""Aceite do de-stub de measure/gate: a régua real dentro do grafo, com a
política em `config/graph.toml` — e os toggles desligados reproduzindo o stub.
"""

import sqlite3
from pathlib import Path

import pytest

from harness.graph.run_graph import GraphPolicy, load_policy, run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV

# Genoma do ALVO (vai junto com a unidade para o workspace): um arquivo na
# blocklist basta para o fingerprint do provision ter o que provar.
GENOME_TOML = 'immutable = ["guarded.txt"]\nmutable = ["notas/**"]\n'
KPIS_TOML = '[kpi.score]\ncmd = "cat score.txt"\ndirection = "higher"\n'


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    # Sem override de config: vale o config/ do repo (graph.toml de fábrica).
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    return d


def _unit(tmp_path: Path, name: str, verify_cmd: str, files=()) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nprompt = "x"\nverify_cmd = "{verify_cmd}"\n',
        encoding="utf-8",
    )
    for rel, content in files:
        p = unit / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return unit


def _count(db: Path, sql: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchone()[0]


# --- (a) defaults ------------------------------------------------------------


def test_policy_defaults_sem_arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "nao-existe"))
    assert load_policy() == GraphPolicy(
        max_attempts=2, verify_timeout_s=120.0, measure=True, tamper=True
    )


def test_policy_nunca_quebra_com_config_torta(tmp_path):
    p = tmp_path / "graph.toml"
    p.write_text("isto nao e toml [", encoding="utf-8")
    assert load_policy(p) == GraphPolicy()

    # Campo torto cai no default DAQUELE campo; o resto do arquivo ainda vale.
    p.write_text(
        'max_attempts = "muitos"\nverify_timeout_s = -3\n'
        '[nodes]\nmeasure = "sim"\ntamper = false\n',
        encoding="utf-8",
    )
    assert load_policy(p) == GraphPolicy(tamper=False)


def test_policy_do_repo_e_a_de_fabrica():
    # O graph.toml versionado no repo tem que ser exatamente os defaults:
    # mudar a política de fábrica é decisão, não efeito colateral.
    repo = Path(__file__).resolve().parent.parent
    assert load_policy(repo / "config" / "graph.toml") == GraphPolicy()


# --- (b) tamper => revert ----------------------------------------------------


def test_tamper_no_imutavel_vira_revert(data_dir, tmp_path):
    # O verify mexe num arquivo da blocklist do genoma do alvo. O fingerprint
    # do provision denuncia no gate: revert, mesmo com verify verde.
    unit = _unit(
        tmp_path,
        "tampered",
        "echo x >> guarded.txt",
        files=(
            ("guarded.txt", "original\n"),
            ("config/genome.toml", GENOME_TOML),
        ),
    )
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-tamper")

    assert final["decision"].action == "revert"
    assert final["decision"].reason.startswith("tamper:")
    assert any("immutable_changed" in v for v in final["tamper"])

    row = store.history()[0]
    assert row.ok is False
    assert row.exit_reason.startswith("tamper:")


# --- (c) verify vermelho => retry, depois escalate ---------------------------


def test_verify_vermelho_retry_depois_escalate(data_dir, tmp_path):
    unit = _unit(tmp_path, "vermelho", "test -f nao_existe.txt")
    final = run_unit(
        unit, "mock", None, data_dir, thread_id="t-esc", max_attempts=2
    )

    assert final["decision"].action == "escalate_human"
    assert final["attempt"] == 1
    gates = [e for e in final["events"] if e["node"] == "gate"]
    assert [g["action"] for g in gates] == ["retry", "escalate_human"]
    # O motivo é o da régua real, não o do stub.
    assert gates[0]["reason"].startswith("verify_failed:exit=")
    assert store.history()[0].exit_reason == "verify_failed"


# --- regressão de KPI => revert ----------------------------------------------


def test_kpi_regressao_vira_revert(data_dir, tmp_path):
    # Verify verde, mas o score caiu entre o antes (provision) e o depois:
    # a régua reverte — verde no verify não compra regressão de KPI.
    unit = _unit(
        tmp_path,
        "piorou",
        "echo 5 > score.txt",
        files=(("kpis.toml", KPIS_TOML), ("score.txt", "10\n")),
    )
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-kpi")

    assert final["kpi_before"] == {"score": 10.0}
    assert final["kpi_after"] == {"score": 5.0}
    assert final["decision"].action == "revert"
    assert final["decision"].reason == "kpi_regression:score"
    assert store.history()[0].exit_reason == "kpi_regression:score"


# --- (d) toggles off == stub antigo ------------------------------------------


def test_toggles_off_reproduzem_stub(data_dir, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "graph.toml").write_text(
        "[nodes]\nmeasure = false\ntamper = false\n", encoding="utf-8"
    )
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))

    # Mesma unidade adulterada + KPI piorando dos testes acima: com os toggles
    # desligados o gate volta a ser o stub e aceita só pelo verify.
    unit = _unit(
        tmp_path,
        "cego",
        "echo x >> guarded.txt && echo 5 > score.txt",
        files=(
            ("guarded.txt", "original\n"),
            ("config/genome.toml", GENOME_TOML),
            ("kpis.toml", KPIS_TOML),
            ("score.txt", "10\n"),
        ),
    )
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-cego")

    assert final["decision"].action == "accept"
    assert final["tamper"] == []
    assert final["kpi_before"] == {} and final["kpi_after"] == {}
    # O stub não grava measure no ledger.
    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node='measure'") == 0
    assert store.history()[0].ok is True
