"""Exame/screening não depositam gabarito na memória episódica global.

O verificador de uma unidade selada imprime o esperado no tail; `_verify` grava
esse tail em `episodic_failures` (banco GLOBAL) e o recall injeta em prompts
futuros do mesmo kind. Isso é o juiz alimentando a memória do avaliado: uma
falha de exame ensinaria o gabarito pro executor.
"""

import os
import sqlite3
from pathlib import Path

import pytest

from harness.improve import coevolve, exam
from harness.ledger import store
from harness.memory import episodic

SECRET = "SEGREDO_gabarito_42"

LEAKY_TOML = f"""\
id = "{{uid}}"
kind = "code"
prompt = "escreva a saída"
verify_cmd = "echo esperado={SECRET}; exit 3"
"""


@pytest.fixture
def global_dir(tmp_path, monkeypatch):
    """Data dir global (namespace da episódica) isolado por teste."""
    d = tmp_path / "global"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _mk_unit(parent: Path, name: str) -> None:
    unit = parent / name
    unit.mkdir(parents=True)
    (unit / "unit.toml").write_text(LEAKY_TOML.format(uid=name), encoding="utf-8")


def _traces(global_dir: Path) -> list[str]:
    """Todo trace no índice episódico global. Banco/tabela ausente = []."""
    db = global_dir / store.DB_NAME
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        try:
            rows = conn.execute(f"SELECT trace FROM {episodic.TABLE}").fetchall()
        except sqlite3.OperationalError:
            return []
    return [r[0] for r in rows]


def test_exame_nao_grava_gabarito_na_episodica(tmp_path, global_dir):
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_leak")

    report = exam.exam_report(sealed_dir=sealed, data_dir=tmp_path / "exp")

    assert report == [{"id": "u_leak", "passed": False}]
    assert not any(SECRET in t for t in _traces(global_dir))
    # A env volta como estava: exame não desliga a memória do resto do processo.
    assert episodic.ENV_ENABLED not in os.environ
    assert episodic.record_failure("code", "depois", "falha normal") is True


def test_screening_nao_grava_gabarito_na_episodica(tmp_path, global_dir):
    quarantine = tmp_path / "quarantine"
    _mk_unit(quarantine, "q_leak")

    frontier = coevolve.screen_quarantine(quarantine_dir=quarantine, data_dir=tmp_path / "exp")

    assert frontier == ["q_leak"]
    assert not any(SECRET in t for t in _traces(global_dir))
