"""CLI `harness frontier`: candidato que falha aparece na saída e na contagem."""

from pathlib import Path

import pytest

PASS_TOML = """\
id = "{uid}"
kind = "code"
prompt = "escreva a saída"
verify_cmd = "test -f mock_output.txt"
"""

FAIL_TOML = """\
id = "{uid}"
kind = "code"
prompt = "escreva a saída"
verify_cmd = "test -f nao_existe.txt"
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _mk_unit(quarantine: Path, name: str, template: str) -> Path:
    unit = quarantine / name
    unit.mkdir(parents=True)
    (unit / "unit.toml").write_text(template.format(uid=name), encoding="utf-8")
    return unit


def test_frontier_lista_candidato_que_falha(tmp_path, data_dir, monkeypatch, capsys):
    from harness.cli import main
    from harness.improve import coevolve

    monkeypatch.chdir(tmp_path)
    q = Path("benchmarks/quarantine")
    # coevolve importa QUARANTINE_DIR no topo: patch no módulo que o usa.
    monkeypatch.setattr(coevolve, "QUARANTINE_DIR", q)
    _mk_unit(q, "q_bad", FAIL_TOML)
    _mk_unit(q, "q_ok", PASS_TOML)

    assert main(["frontier"]) == 0

    out = capsys.readouterr().out
    assert "q_bad" in out
    assert "q_ok" not in out
    assert "frontier=1" in out
