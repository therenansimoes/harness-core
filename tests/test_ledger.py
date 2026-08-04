import sqlite3

import pytest

from harness.ledger import store
from harness.types import MutationRow, RunRow


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


# Schema de `runs` como era antes de `tokens_in`/`tokens_out` — banco legado que
# a migração tem de aceitar sem perder as linhas já gravadas.
OLD_RUNS_SCHEMA = """
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    unit_id       TEXT    NOT NULL,
    project       TEXT,
    backend       TEXT    NOT NULL,
    model         TEXT,
    tier          TEXT,
    kind          TEXT,
    ok            INTEGER NOT NULL,
    exit_reason   TEXT    NOT NULL,
    sec_total     REAL    NOT NULL,
    sec_provision REAL    NOT NULL,
    cost_usd      REAL,
    intervention  INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);
"""


def test_tokens_roundtrip(data_dir):
    """Usage em coluna própria: custo em dólar depende da tabela de preço, token
    não. Backend que não reporta usage continua gravando NULL."""
    store.record_run(_row(run_id="com", tokens_in=1200, tokens_out=340))
    store.record_run(_row(run_id="sem"))

    by_id = {r.run_id: r for r in store.history()}
    assert (by_id["com"].tokens_in, by_id["com"].tokens_out) == (1200, 340)
    assert by_id["sem"].tokens_in is None
    assert by_id["sem"].tokens_out is None


def test_migracao_adiciona_colunas_de_token(tmp_path):
    """Banco velho ganha as colunas no open; linha antiga fica NULL (não há de
    onde fazer backfill) e a nova grava o usage normalmente."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_RUNS_SCHEMA)
    conn.execute(
        "INSERT INTO runs (run_id, unit_id, project, backend, model, tier, kind, "
        "ok, exit_reason, sec_total, sec_provision, cost_usd, intervention, "
        "created_at) VALUES ('velha', 'u', 'p', 'mock', NULL, 'tier0', 'code', "
        "1, 'done', 1.0, 0.1, 0.02, 0, 't')"
    )
    conn.commit()
    conn.close()

    store.record_run(_row(run_id="nova", tokens_in=10, tokens_out=20), path=path)

    by_id = {r.run_id: r for r in store.history(path=path)}
    assert by_id["velha"].tokens_in is None
    assert by_id["velha"].cost_usd == 0.02   # a linha antiga continua legível
    assert (by_id["nova"].tokens_in, by_id["nova"].tokens_out) == (10, 20)


def _mut(i: int, rule: str = "floor_up") -> MutationRow:
    return MutationRow(f"m{i:04d}", rule, "KEEP", "1/6", "4/6", store.now_iso(), False)


def test_mutations_sem_teto_le_o_historico_inteiro(data_dir):
    """`limit=None` = todas. O guard de config sujo pergunta "este id já foi
    julgado?", e a janela default responderia "não" para tudo que envelheceu."""
    for i in range(600):
        store.record_mutation(_mut(i))

    assert len(store.mutations()) == 500              # janela default
    assert len(store.mutations(limit=None)) == 600
    assert store.mutations(limit=None)[-1].mutation_id == "m0000"
    assert len(store.mutations(rule_id="floor_up", limit=None)) == 600
    assert store.mutations(rule_id="outra", limit=None) == []
