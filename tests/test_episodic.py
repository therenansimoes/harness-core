"""Ponta de escrita da memória episódica: verify vermelho tem que virar caso.

O recall já era consumido no prompt (`deepagents_backend._episodic_block`); sem
estes testes o banco podia ficar vazio para sempre e ninguém notaria.
"""

from pathlib import Path

import pytest

from harness import cli
from harness.graph.run_graph import run_unit
from harness.memory import episodic

# Marca única no log do verify: é ela que o recall tem que devolver.
BOOM = "BOOM_episodio"
VERIFY_LOUD = f"echo {BOOM}; exit 3"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _unit(tmp_path: Path, name: str, kind: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nkind = "{kind}"\nprompt = "x"\nverify_cmd = "{VERIFY_LOUD}"\n',
        encoding="utf-8",
    )
    return unit


def test_graph_verify_failure_is_recalled(data_dir, tmp_path):
    unit = _unit(tmp_path, "bad", "code")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-ep", max_attempts=1)
    assert final["verdict"].passed is False

    traces = episodic.recall("code", BOOM)
    assert traces and BOOM in traces[0]


def test_run_once_verify_failure_is_recalled(data_dir, tmp_path):
    """`harness run` não passa pelo grafo: o hook do cli.run_once é outro ponto."""
    unit = _unit(tmp_path, "loud", "code")
    assert cli.main(["run", "--unit", str(unit), "--backend", "mock"]) == 1

    traces = episodic.recall("code", BOOM)
    assert traces and BOOM in traces[0]


def test_recall_is_keyed_by_kind(data_dir, tmp_path):
    """Caso de outro kind não contamina o prompt de quem não pediu."""
    run_unit(
        _unit(tmp_path, "bad", "code"), "mock", None, data_dir, thread_id="t-kind", max_attempts=1
    )

    assert episodic.recall("code", BOOM)
    assert episodic.recall("docs", BOOM) == []
