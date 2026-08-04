"""Gate de delta: retry só continua enquanto a nota graduada sobe."""

from pathlib import Path

import pytest

from harness.graph.run_graph import CFG_DATA_DIR, GRAPH_TOML, _gate
from harness.graph.state import Budget
from harness.improve.escalate import NO_GRADIENT
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Check, UnitSpec, Verdict

RUN_ID = "r-delta"


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Path:
    """config/ vazio: `load_policy` cai nos defaults (delta_gate ligado)."""
    c = tmp_path / "config"
    c.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(c))
    return c


def _unit(tmp_path: Path, checks: tuple[Check, ...]) -> UnitSpec:
    return UnitSpec(
        id="u", path=tmp_path / "u", prompt="x", verify_cmd="true", checks=checks
    )


def _state(tmp_path: Path, attempt: int, unit: UnitSpec) -> dict:
    return {
        "run_id": RUN_ID,
        "unit": unit,
        "workspace": str(tmp_path / "ws"),
        "attempt": attempt,
        "budget": Budget(max_attempts=9),   # teto alto: o corte tem que ser o delta
        "verdict": Verdict(
            passed=False, exit_code=64, log_path=tmp_path / "v.log", sec=0.1,
            score=0.5, failed=("c1",),
        ),
    }


def _seed_scores(db: Path, scores: list[float]) -> None:
    """Payload do verify por tentativa, como `_verify` grava."""
    for attempt, score in enumerate(scores):
        store.record_node(
            RUN_ID,
            "verify",
            {"passed": False, "exit_code": 64, "log_path": "v.log", "sec": 0.1,
             "score": score, "failed": ["c1"]},
            db,
            attempt=attempt,
        )


def _config(data_dir: Path) -> dict:
    return {"configurable": {CFG_DATA_DIR: str(data_dir)}}


CHECKS = (Check(name="c1", cmd="true", weight=1.0),)


def test_score_subindo_continua_em_retry(tmp_path, data_dir, cfg):
    db = data_dir / store.DB_NAME
    _seed_scores(db, [0.2, 0.4, 0.6])

    out = _gate(_state(tmp_path, 2, _unit(tmp_path, CHECKS)), _config(data_dir))

    assert out["decision"].action == "retry"
    assert "sem_gradiente" not in out["decision"].reason


def test_uma_estagnacao_ainda_e_ruido(tmp_path, data_dir, cfg):
    db = data_dir / store.DB_NAME
    _seed_scores(db, [0.4, 0.4, 0.6])   # parou uma vez e voltou a subir

    out = _gate(_state(tmp_path, 2, _unit(tmp_path, CHECKS)), _config(data_dir))

    assert out["decision"].action == "retry"


def test_duas_estagnacoes_escalam_com_os_dois_scores(tmp_path, data_dir, cfg):
    db = data_dir / store.DB_NAME
    _seed_scores(db, [0.6, 0.5, 0.5])   # não subiu na 1 nem na 2

    out = _gate(_state(tmp_path, 2, _unit(tmp_path, CHECKS)), _config(data_dir))

    assert out["decision"].action == "escalate_human"
    assert "sem_gradiente_de_score" in out["decision"].reason
    event = out["events"][0]
    assert event["score_prev"] == 0.5 and event["score_now"] == 0.5
    assert event["stagnations"] == 2
    # Motivo agrupável vem do vocabulário fechado da escalação, não texto novo.
    assert event["escalate_reason"] == NO_GRADIENT


def test_unidade_sem_checks_intacta(tmp_path, data_dir, cfg):
    """Sem `[checks]` não há régua graduada: retry cego de sempre, bit a bit."""
    db = data_dir / store.DB_NAME
    _seed_scores(db, [1.0, 1.0, 1.0])   # score fixo do caminho binário

    out = _gate(_state(tmp_path, 2, _unit(tmp_path, ())), _config(data_dir))

    assert out["decision"].action == "retry"
    assert "score_prev" not in out["events"][0]


def test_knob_desligado_intacto(tmp_path, data_dir, cfg):
    (cfg / GRAPH_TOML).write_text("[nodes]\ndelta_gate = false\n", encoding="utf-8")
    db = data_dir / store.DB_NAME
    _seed_scores(db, [0.6, 0.5, 0.5])

    out = _gate(_state(tmp_path, 2, _unit(tmp_path, CHECKS)), _config(data_dir))

    assert out["decision"].action == "retry"
    assert "score_prev" not in out["events"][0]


def test_teto_de_tentativas_continua_mandando(tmp_path, data_dir, cfg):
    """Budget esgotado escala pelo motivo dele — o delta não substitui o teto."""
    db = data_dir / store.DB_NAME
    _seed_scores(db, [0.2, 0.4])
    state = _state(tmp_path, 1, _unit(tmp_path, CHECKS))
    state["budget"] = Budget(max_attempts=2)

    out = _gate(state, _config(data_dir))

    assert out["decision"].action == "escalate_human"
    assert "acabaram as 2 tentativas" in out["decision"].reason
    assert "sem_gradiente_de_score" not in out["decision"].reason
