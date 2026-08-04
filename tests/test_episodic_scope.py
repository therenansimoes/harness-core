"""Escopo da memória episódica: global entre experimentos, e desligável.

Regressão de dois jeitos de perder a memória sem ninguém notar: gravar no
`runs.sqlite` do experimento (a leitura é global, então o caso vira invisível) e
não ter kill switch quando a memória atrapalha.
"""

from pathlib import Path

import pytest

from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.memory import episodic

BOOM = "BOOM_escopo"
VERIFY_LOUD = f"echo {BOOM}; exit 3"


def _unit(tmp_path: Path, name: str, kind: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nkind = "{kind}"\nprompt = "x"\nverify_cmd = "{VERIFY_LOUD}"\n',
        encoding="utf-8",
    )
    return unit


def _episodic_rows(db: Path) -> int:
    """Linhas em `episodic_failures` no db dado. Tabela ausente = 0 — é o caso
    esperado num db de experimento que nunca deveria ter recebido episódio."""
    import sqlite3

    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        try:
            return conn.execute(f"SELECT count(*) FROM {episodic.TABLE}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def test_write_is_global_not_run_db(tmp_path, monkeypatch):
    """Run com data dir próprio (experimento) grava o episódio no ledger GLOBAL."""
    tmp_global = tmp_path / "global"
    tmp_exp = tmp_path / "exp"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_global))

    unit = _unit(tmp_path, "bad", "code")
    final = run_unit(unit, "mock", None, tmp_exp, thread_id="t-scope", max_attempts=1)
    assert final["verdict"].passed is False

    # Leitura global (sem db_path) enxerga o caso...
    traces = episodic.recall("code", BOOM)
    assert traces and BOOM in traces[0]
    # ...e o db do experimento não virou dono do episódio.
    assert _episodic_rows(tmp_exp / store.DB_NAME) == 0


def test_kill_switch_off(tmp_path, monkeypatch):
    """`HARNESS_EPISODIC=0` cala escrita e leitura; tirar a env volta tudo."""
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "global"))
    assert episodic.record_failure("code", "u1", BOOM) is True

    monkeypatch.setenv("HARNESS_EPISODIC", "0")
    assert episodic.record_failure("code", "u2", BOOM) is False
    assert episodic.recall("code", BOOM) == []

    monkeypatch.delenv("HARNESS_EPISODIC")
    traces = episodic.recall("code", BOOM)
    assert traces and BOOM in traces[0]


@pytest.mark.parametrize("val", ["0", "off", "FALSE", "No"])
def test_kill_switch_values(monkeypatch, val):
    monkeypatch.setenv("HARNESS_EPISODIC", val)
    assert episodic.recall("code", BOOM) == []
