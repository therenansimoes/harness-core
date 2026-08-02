import pytest

from harness.ledger import store
from harness.types import RunRow


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _row(**over) -> RunRow:
    base = dict(
        run_id="r1", unit_id="echo", project="p", backend="mock", model=None,
        tier="tier0", kind="code", ok=True, exit_reason="done", sec_total=1.5,
        sec_provision=0.1, cost_usd=0.0, intervention=False,
        created_at=store.now_iso(),
    )
    base.update(over)
    return RunRow(**base)


def test_creates_db_dir(data_dir):
    store.record_run(_row())
    assert (data_dir / "runs.sqlite").is_file()


def test_roundtrip(data_dir):
    rid = store.record_run(_row())
    assert rid == 1

    rows = store.history()
    assert len(rows) == 1
    got = rows[0]
    assert got.id == rid
    assert got.run_id == "r1"
    assert got.backend == "mock"
    assert got.kind == "code"
    assert got.ok is True
    assert got.intervention is False
    assert got.sec_total == 1.5


def test_filters_and_order(data_dir):
    store.record_run(_row(run_id="a", kind="code", backend="mock"))
    store.record_run(_row(run_id="b", kind="content", backend="mock"))
    store.record_run(_row(run_id="c", kind="code", backend="other", project="q"))

    assert [r.run_id for r in store.history()] == ["c", "b", "a"]
    assert [r.run_id for r in store.history(kind="code")] == ["c", "a"]
    assert [r.run_id for r in store.history(backend="mock", kind="code")] == ["a"]
    assert [r.run_id for r in store.history(project="q")] == ["c"]
    assert len(store.history(limit=1)) == 1
