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


def test_node_events_are_keyed_by_attempt(data_dir):
    assert store.record_node("r1", "execute", {"n": 0}) is True
    assert store.record_node("r1", "execute", {"n": 0}) is False
    assert store.record_node("r1", "execute", {"n": 1}, attempt=1) is True

    assert store.get_node("r1", "execute") == {"n": 0}
    assert store.get_node("r1", "execute", attempt=1) == {"n": 1}
    assert store.get_node("r1", "execute", attempt=2) is None


def test_record_run_once_dedups_the_row(data_dir):
    rid, wrote = store.record_run_once(_row())
    assert wrote is True
    assert store.get_node("r1", "record") == {"row_id": rid}

    again, wrote_again = store.record_run_once(_row())
    assert (again, wrote_again) == (rid, False)
    assert len(store.history()) == 1


def test_record_run_once_rolls_back_row_if_marker_fails(data_dir, monkeypatch):
    # Crash entre as duas escritas não pode deixar a linha do run órfã: sem
    # marcador, o resume inseriria uma segunda linha para o mesmo run.
    def boom(*args, **kwargs):
        raise RuntimeError("morreu entre o INSERT e o marcador")

    # context(): undo() desfaria TAMBÉM o HARNESS_DATA_DIR da fixture, e o
    # history() abaixo leria o data/ real do repo.
    with monkeypatch.context() as m:
        m.setattr(store, "_insert_node", boom)
        with pytest.raises(RuntimeError):
            store.record_run_once(_row())

    assert store.history() == []
    assert store.get_node("r1", "record") is None


def test_filters_and_order(data_dir):
    store.record_run(_row(run_id="a", kind="code", backend="mock"))
    store.record_run(_row(run_id="b", kind="content", backend="mock"))
    store.record_run(_row(run_id="c", kind="code", backend="other", project="q"))

    assert [r.run_id for r in store.history()] == ["c", "b", "a"]
    assert [r.run_id for r in store.history(kind="code")] == ["c", "a"]
    assert [r.run_id for r in store.history(backend="mock", kind="code")] == ["a"]
    assert [r.run_id for r in store.history(project="q")] == ["c"]
    assert len(store.history(limit=1)) == 1
