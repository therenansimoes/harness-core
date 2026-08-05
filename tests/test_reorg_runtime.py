"""Reorg no runtime: o que o grafo APLICA e o que ele apenas grava.

Mesmo cenário de backends falsos do `test_run_graph` — nenhum modelo de verdade
é chamado aqui. O sinal entra pelo ledger (linhas de run semeadas na mão), que é
exatamente a fonte que o `_route` e o `_gate` leem em produção.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from harness.backends import registry
from harness.governor import reorg
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight, RunRow

REPO = Path(__file__).resolve().parent.parent
SPY_OUTPUT = "spy.txt"

MODELS_TOML = """\
[[tier]]
name = "t0"
backend = "spy0"
model = "m0"
max_turns = 3
cost_rank = 0

[[tier]]
name = "t1"
backend = "spy1"
model = "m1"
max_turns = 5
cost_rank = 1

[router]
default_tier = "t0"
max_attempts = 3
min_n = 6
prior_floor = 0.50

[router.kind]
code = "t0"
"""


class SpyBackend:
    """Anota quem foi chamado com qual modelo: é o que torna o delta de tier do
    reorg observável — o nome do tier no evento não prova quem executou."""

    def __init__(self, name: str, calls: list[tuple]) -> None:
        self.name = name
        self.calls = calls

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=True,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append((self.name, req.model, req.max_turns))
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / SPY_OUTPUT).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(SPY_OUTPUT,),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def auto_config(tmp_path, monkeypatch):
    """config/ só deste teste: tiers falsos e NENHUM governor.toml — reorg cai
    nos defaults congelados, que é o caminho que se quer exercitar."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    shutil.copy(REPO / "config" / "kinds.toml", cfg / "kinds.toml")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


@pytest.fixture
def spies(auto_config):
    calls: list[tuple] = []
    names = ("spy0", "spy1")
    for name in names:
        registry.register(name, (lambda n: lambda: SpyBackend(n, calls))(name))
    yield calls
    for name in names:
        registry.unregister(name)


def _unit(tmp_path: Path, name: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nkind = "code"\nprompt = "x"\nverify_cmd = "test -f {SPY_OUTPUT}"\n',
        encoding="utf-8",
    )
    return unit


def _seed_falhas(db: Path, n: int = 2, classe: str = "verify_failed") -> None:
    """`n` runs REPROVADAS da mesma classe, do mesmo kind — o sinal do R1."""
    for i in range(n):
        store.record_run(
            RunRow(
                run_id=f"seed{i}",
                unit_id=f"velha{i}",
                project=None,
                backend="spy0",
                model="m0",
                tier="t0",
                kind="code",
                ok=False,
                exit_reason=classe,
                sec_total=1.0,
                sec_provision=0.0,
                cost_usd=0.0,
                intervention=False,
                created_at=store.now_iso(),
            ),
            path=db,
        )


def _reorg_rows(db: Path) -> list[dict]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT payload FROM node_events WHERE node = ? ORDER BY rowid", (reorg.NODE,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_regra_dispara_grava_node_reorg_com_sinal(data_dir, tmp_path, spies):
    """Decisão de topologia sem a evidência que a causou não é auditável: o
    payload carrega o sinal, não só o nome da regra."""
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    run_unit(_unit(tmp_path, "sinal"), None, None, data_dir, thread_id="t-sinal", route="auto")

    rows = _reorg_rows(db)
    assert [r["rule_id"] for r in rows] == [reorg.R_ESCALATE]
    assert rows[0]["state"] == "applied"
    assert rows[0]["effect"] == "applied"
    assert rows[0]["signal"] == {"failure_class": "verify_failed", "count": 2}
    assert rows[0]["run_id"] == "t-sinal"


def test_tier_sobe_um_nivel_quando_r1(data_dir, tmp_path, spies):
    """Duas falhas da mesma classe no ledger e a PRIMEIRA tentativa já roda um
    tier acima — quem paga muda, e é o backend chamado que prova."""
    _seed_falhas(data_dir / store.DB_NAME)
    final = run_unit(
        _unit(tmp_path, "sobe"), None, None, data_dir, thread_id="t-sobe", route="auto"
    )

    assert final["selection"].tier == "t1"
    assert spies == [("spy1", "m1", 5)]
    assert final["decision"].action == "accept"

    reasons = [r for e in final["events"] if e["node"] == "route" for r in e["reasons"]]
    assert "reorg:escalate_route:t0->t1" in reasons
    assert store.history()[0].backend == "spy1"


def test_sinal_limpa_grava_reversao(data_dir, tmp_path, spies):
    """O sinal sumiu do ledger: a decisão que ele justificava tem que cair.
    Topologia mudada por um sinal morto é dívida, não decisão."""
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    unit = _unit(tmp_path, "revert")
    run_unit(unit, None, None, data_dir, thread_id="t-rev", route="auto")
    assert [r["state"] for r in _reorg_rows(db)] == ["applied"]

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM runs WHERE ok = 0")
    run_unit(unit, None, None, data_dir, thread_id="t-rev", route="auto")

    rows = _reorg_rows(db)
    assert [r["state"] for r in rows] == ["applied", "reverted"]
    assert rows[1]["rule_id"] == reorg.R_ESCALATE
    assert rows[1]["cleared_signal"] == {"failure_class": "verify_failed", "count": 2}


def test_reorg_quebrado_nao_derruba_run(data_dir, tmp_path, spies, monkeypatch):
    """Reorg é opinião sobre o desenho do run. Opinião que estoura não pode
    levar o run junto: sem decisão, o grafo segue como antes de D5 existir."""
    _seed_falhas(data_dir / store.DB_NAME)

    def boom(*_a, **_k):
        raise RuntimeError("reorg pifou")

    monkeypatch.setattr(reorg, "decide", boom)
    final = run_unit(
        _unit(tmp_path, "boom"), None, None, data_dir, thread_id="t-boom", route="auto"
    )

    assert final["decision"].action == "accept"
    assert final["selection"].tier == "t0"  # sem reorg, o tier é o do router
    assert spies == [("spy0", "m0", 3)]
    assert _reorg_rows(data_dir / store.DB_NAME) == []
