"""Aceite do PR-2: kill -9 durante o `execute`, re-invoke no mesmo thread_id,
run completa e o ledger registra exatamente uma execução concluída.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import resume_child  # tests/ está no sys.path do pytest

from harness.backends import registry
from harness.ledger import store

FIXTURE = Path(__file__).parent / "fixtures" / "echo"
CHILD = Path(__file__).parent / "resume_child.py"
ROOT = Path(__file__).resolve().parents[1]

SENTINEL_TIMEOUT_S = 15.0
KILL_AFTER_S = 1.0
THREAD_ID = "t-resume"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def marks(tmp_path):
    """Backend-sonda rápido no processo do teste; some do registry no fim."""
    path = tmp_path / "marks"
    resume_child.register(path, sleep_s=0.0)
    yield path
    registry.unregister(resume_child.BACKEND_NAME)


def _count(db: Path, sql: str) -> int:
    if not db.is_file():
        return 0
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchone()[0]


def _spawn_child(marks: Path, data_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, str(CHILD), str(marks), str(data_dir), str(FIXTURE), THREAD_ID],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_sentinel(proc: subprocess.Popen, marks: Path) -> None:
    deadline = time.monotonic() + SENTINEL_TIMEOUT_S
    sentinel = marks / resume_child.SENTINEL
    while time.monotonic() < deadline:
        if sentinel.is_file():
            return
        if proc.poll() is not None:
            _, err = proc.communicate()
            pytest.fail(f"child morreu antes do execute: {err.decode()[-2000:]}")
        time.sleep(0.05)
    proc.kill()
    pytest.fail("execute não começou dentro do timeout")


def test_sigkill_during_execute_resumes_without_reexecuting(data_dir, marks):
    db = data_dir / store.DB_NAME

    child = _spawn_child(marks, data_dir)
    _wait_for_sentinel(child, marks)
    time.sleep(KILL_AFTER_S)  # o backend lento ainda está dentro do execute
    child.send_signal(signal.SIGKILL)
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL

    # Morreu no meio: chamou o backend, mas nenhuma execução concluiu.
    assert resume_child.count(marks, resume_child.CALLS) == 1
    assert resume_child.count(marks, resume_child.DONE) == 0
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node='execute'") == 0
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node='provision'") == 1

    # Retomada: mesmo thread_id, mesmo data_dir, backend agora rápido.
    from harness.graph.run_graph import run_unit

    final = run_unit(FIXTURE, resume_child.BACKEND_NAME, None, data_dir, THREAD_ID)

    assert final["decision"].action == "accept"
    assert final["exec"].ok is True
    assert resume_child.count(marks, resume_child.DONE) == 1

    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node='execute'") == 1
    assert _count(db, "SELECT COUNT(*) FROM runs") == 1
    row = store.history()[0]
    assert row.run_id == THREAD_ID
    assert row.ok is True

    # Nada de provisionar duas vezes: o workspace do child foi reusado.
    assert Path(final["workspace"]) == data_dir / "ws" / THREAD_ID

    # O trace é de um run só: retomou do checkpoint, não recomeçou do START
    # (recomeçar duplicaria plan/route/provision na lista append-only).
    assert [e["node"] for e in final["events"]] == [
        "plan", "route", "provision", "execute", "verify",
        "measure", "gate", "accept", "record",
    ]
