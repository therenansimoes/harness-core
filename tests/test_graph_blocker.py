"""O gate roteia por TIPO de blocker: humano sem gastar tentativa, ou retry adiado."""

import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from harness.backends import registry
from harness.graph import run_graph
from harness.graph.run_graph import GraphPolicy, load_policy, run_unit
from harness.ledger import store
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

DEFER_S = 0.01  # o teste mede que a espera existe, não quanto ela dura


class BlockerBackend:
    """Declara sempre o mesmo tipo e nunca escreve o que o verify procura."""

    name: ClassVar[str] = "blk"

    def __init__(self, tipo: str, calls: list[str]) -> None:
        self.tipo = tipo
        self.calls = calls

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append(self.tipo)
        return ExecResult(
            ok=False,
            exit_reason="blocker",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(),
            session_id=None,
            trace_path=req.trace_path,
            blocker=self.tipo,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def policy_rapida(monkeypatch):
    """Defer de 10ms: o `_retry` dorme de verdade, e 30s no teste seria pane."""
    monkeypatch.setattr(
        run_graph, "load_policy", lambda *a, **k: GraphPolicy(blocker_defer_s=DEFER_S)
    )


@pytest.fixture
def blk(request):
    calls: list[str] = []
    registry.register("blk", lambda: BlockerBackend(request.param, calls))
    yield calls
    registry.unregister("blk")


def _unit(tmp_path: Path, name: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nprompt = "x"\nverify_cmd = "test -f nao_existe.txt"\n',
        encoding="utf-8",
    )
    return unit


def _count(db: Path, node: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM node_events WHERE node = ?", (node,)).fetchone()[
            0
        ]


def _ev(final, node: str) -> list[dict]:
    return [e for e in final["events"] if e["node"] == node]


@pytest.mark.parametrize("blk", ["needs_user_input"], indirect=True)
def test_needs_user_input_escalona_sem_queimar_a_segunda_tentativa(
    data_dir, tmp_path, blk, policy_rapida
):
    unit = _unit(tmp_path, "pergunta")
    final = run_unit(unit, "blk", None, data_dir, thread_id="t-blk-user", max_attempts=2)

    assert final["decision"].action == "escalate_human"
    assert "blocker:needs_user_input" in final["decision"].reason
    # A tentativa que sobrava NÃO foi gasta: um execute, nenhum retry.
    assert blk == ["needs_user_input"]
    assert final["attempt"] == 0
    assert _count(data_dir / store.DB_NAME, "execute") == 1
    assert _ev(final, "retry") == []
    # E o motivo do teto não entrou na frase: não foi teto que parou o run.
    assert "acabaram as" not in final["decision"].reason

    rows = store.history()
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].exit_reason == "blocker"
    assert _ev(final, "record")[0]["note"] == "blocker:needs_user_input"


@pytest.mark.parametrize("blk", ["external_wait"], indirect=True)
def test_external_wait_segue_em_retry_com_defer_no_evento(data_dir, tmp_path, blk, policy_rapida):
    unit = _unit(tmp_path, "espera")
    final = run_unit(unit, "blk", None, data_dir, thread_id="t-blk-wait", max_attempts=2)

    # O retry continua existindo — só adiado.
    assert blk == ["external_wait", "external_wait"]
    assert _ev(final, "retry")[0]["defer_s"] == DEFER_S
    gate0 = _ev(final, "gate")[0]
    assert gate0["action"] == "retry"
    assert gate0["blocker"] == "external_wait"
    assert gate0["defer_s"] == DEFER_S
    # Segundo gate: o teto de tentativas manda pro humano, e o defer não viaja
    # num caminho que não vai retentar.
    gate1 = _ev(final, "gate")[1]
    assert gate1["action"] == "escalate_human"
    assert "defer_s" not in gate1


def test_blocker_defer_s_vem_do_graph_toml(tmp_path):
    p = tmp_path / "graph.toml"
    p.write_text("blocker_defer_s = 5\n", encoding="utf-8")
    assert load_policy(p).blocker_defer_s == 5.0
    # Campo torto cai no default, como o resto da política.
    p.write_text('blocker_defer_s = "logo"\n', encoding="utf-8")
    assert load_policy(p).blocker_defer_s == GraphPolicy().blocker_defer_s
