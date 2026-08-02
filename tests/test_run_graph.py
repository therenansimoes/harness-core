import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from harness.graph import checkpoint  # noqa: E402
from harness.graph.run_graph import run_unit  # noqa: E402
from harness.ledger import store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "echo"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _count(db: Path, sql: str, *params) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_run_unit_accepts_and_records(data_dir):
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-accept")

    assert final["decision"].action == "accept"
    assert final["verdict"].passed is True
    assert final["exec"].ok is True
    assert final["run_id"] == "t-accept"
    assert Path(final["workspace"]).is_dir()

    rows = store.history()
    assert len(rows) == 1
    assert rows[0].run_id == "t-accept"
    assert rows[0].backend == "mock"
    assert rows[0].kind == "code"
    assert rows[0].ok is True
    assert rows[0].exit_reason == "done"

    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 1
    assert store.get_node("t-accept", "execute")["exit_reason"] == "done"


def test_events_are_the_trace(data_dir):
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-trace")
    nodes = [e["node"] for e in final["events"]]
    assert nodes == [
        "plan", "route", "provision", "execute", "verify",
        "measure", "gate", "accept", "record",
    ]


def test_failed_verify_escalates_after_max_attempts(data_dir, tmp_path):
    unit = tmp_path / "bad"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "bad"\nprompt = "x"\nverify_cmd = "test -f nao_existe.txt"\n',
        encoding="utf-8",
    )
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-fail", max_attempts=2)

    assert final["decision"].action == "escalate_human"
    assert final["attempt"] == 1
    rows = store.history()
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].exit_reason == "verify_failed"


def test_reinvoke_of_finished_thread_keeps_one_ledger_row(data_dir):
    # Nó já marcado em node_events não repete escrita externa, mesmo em re-run.
    run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-again")
    run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-again")

    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM runs") == 1
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 1


def test_provision_reuses_workspace(data_dir):
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-ws")
    ws = Path(final["workspace"])
    assert ws == data_dir / "ws" / "t-ws"

    marker = ws / "sobrevivi.txt"
    marker.write_text("x", encoding="utf-8")
    again = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-ws")
    assert Path(again["workspace"]) == ws
    assert marker.is_file()


def test_checkpointer_writes_its_own_db(data_dir):
    run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-cp")
    assert checkpoint.checkpoint_path(data_dir).is_file()


def test_bootstrap_locks_serde_and_kills_tracing(monkeypatch):
    for var in ("LANGGRAPH_STRICT_MSGPACK", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
        monkeypatch.delenv(var, raising=False)
    checkpoint.bootstrap_env()
    import os

    assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["LANGSMITH_TRACING"] == "false"
