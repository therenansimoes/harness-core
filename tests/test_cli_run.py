import os
import sqlite3
from pathlib import Path

import pytest

from harness import cli
from harness.ledger import store

FIXTURE = str(Path(__file__).parent / "fixtures" / "echo")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_run_mock_exits_zero_and_records_one_row(data_dir):
    rc = cli.main(["run", "--unit", FIXTURE, "--backend", "mock"])
    assert rc == 0

    db = data_dir / "runs.sqlite"
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    row = store.history()[0]
    assert row.unit_id == "echo"
    assert row.backend == "mock"
    assert row.kind == "code"
    assert row.ok is True
    assert row.exit_reason == "done"
    assert row.cost_usd == 0.0
    assert row.intervention is False


def test_run_records_failure_when_verify_fails(data_dir, tmp_path):
    unit = tmp_path / "bad"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "bad"\nprompt = "x"\nverify_cmd = "test -f nao_existe.txt"\n',
        encoding="utf-8",
    )
    assert cli.main(["run", "--unit", str(unit), "--backend", "mock"]) == 1
    assert store.history()[0].exit_reason == "verify_failed"


def test_unknown_backend_raises(data_dir):
    with pytest.raises(KeyError):
        cli.main(["run", "--unit", FIXTURE, "--backend", "nao-existe"])


def test_bootstrap_disables_tracing(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    cli._bootstrap()
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_backends_lists_mock(data_dir, capsys):
    assert cli.main(["backends"]) == 0
    assert "mock" in capsys.readouterr().out
