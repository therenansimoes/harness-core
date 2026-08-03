"""Coluna `action` em `mutations`: migração do banco antigo e placar por ação.

O nome da ação viajava dentro do `note` (`action=<nome>;resto`), então o placar
era global. A coluna própria só serve se as duas eras do ledger conviverem: um
banco gravado antes dela precisa ganhar a coluna no open e recuperar o que der
do note; o novo grava direto.
"""

import sqlite3

import pytest

from harness import cli
from harness.improve import policy
from harness.ledger import store
from harness.types import MutationRow

# Schema de `mutations` como era antes da coluna `action` — o banco legado que a
# migração tem de aceitar.
OLD_SCHEMA = """
CREATE TABLE mutations (
    mutation_id TEXT    PRIMARY KEY,
    rule_id     TEXT    NOT NULL,
    verdict     TEXT    NOT NULL,
    arm_a       TEXT    NOT NULL,
    arm_b       TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    reverted    INTEGER NOT NULL,
    note        TEXT
);
"""


def _mutation(mid: str, verdict: str, **over) -> MutationRow:
    base = dict(
        mutation_id=mid, rule_id="r1", verdict=verdict, arm_a="3/6", arm_b="5/6",
        applied_at="2026-01-01T00:00:00+00:00", reverted=(verdict != "KEEP"),
    )
    return MutationRow(**{**base, **over})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


def _legacy_db(tmp_path):
    """Banco só com o schema velho de `mutations` e duas linhas gravadas."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO mutations (mutation_id, rule_id, verdict, arm_a, arm_b, "
        "applied_at, reverted, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("m1", "r1", "KEEP", "3/6", "5/6", "t", 0, "action=research;ganho"),
            ("m2", "r1", "DISCARD", "3/6", "1/6", "t", 1, "deadline"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_migracao_adiciona_coluna_e_faz_backfill_do_note(tmp_path):
    path = _legacy_db(tmp_path)

    rows = store.mutations(path=path)

    cols = {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(mutations)"
    )}
    assert "action" in cols
    by_id = {r.mutation_id: r for r in rows}
    # note com token: a ação sai do texto livre e vai para a coluna
    assert by_id["m1"].action == "research"
    assert by_id["m1"].note == "action=research;ganho"
    # note sem token: NULL, e o placar cai no fallback (que também dá None)
    assert by_id["m2"].action is None
    assert policy.action_of(by_id["m2"]) is None


def test_migracao_idempotente_no_reopen(tmp_path):
    path = _legacy_db(tmp_path)

    for _ in range(3):
        with store.connect(path):
            pass

    # segunda passada não duplica coluna nem reescreve linha
    assert [r.action for r in store.mutations(path=path)] == [None, "research"]
    store.record_mutation(_mutation("m3", "KEEP", action="codegen"), path=path)
    assert store.mutations(path=path)[0].action == "codegen"


def test_banco_novo_grava_e_le_a_coluna(tmp_path):
    path = tmp_path / "novo.sqlite"

    store.record_mutation(_mutation("m1", "KEEP", action="research"), path=path)
    store.record_mutation(_mutation("m2", "KEEP"), path=path)

    by_id = {r.mutation_id: r for r in store.mutations(path=path)}
    assert by_id["m1"].action == "research"
    assert by_id["m2"].action is None


def test_actions_placar_por_acao(env, capsys):
    store.record_mutation(_mutation("m1", "KEEP", action="research"))
    store.record_mutation(_mutation("m2", "DISCARD", action="research"))
    store.record_mutation(_mutation("m3", "KEEP", action="codegen"))
    # linha da era antiga: sem coluna, ação só no note — o fallback a conta
    store.record_mutation(_mutation("m4", "KEEP", note="action=codegen;x"))

    rc = cli.main(["actions"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "research KEEP=1 DISCARD=1" in out
    assert "codegen KEEP=2 DISCARD=0" in out
    assert "mutações=4" in out
