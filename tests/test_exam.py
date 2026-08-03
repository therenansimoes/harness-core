"""Exame selado real: fail-closed em vazio/falha/exceção, True só com tudo verde."""

from pathlib import Path

import pytest

from harness.improve import exam

REPO = Path(__file__).resolve().parent.parent

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


def _mk_unit(sealed: Path, name: str, template: str) -> None:
    unit = sealed / name
    unit.mkdir(parents=True)
    (unit / "unit.toml").write_text(template.format(uid=name), encoding="utf-8")


def test_sealed_vazio_false_com_stderr(tmp_path, data_dir, capsys):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    assert exam.run_sealed_exam(sealed_dir=sealed, data_dir=data_dir) is False
    assert "sem unidades" in capsys.readouterr().err


def test_todas_passam_true(tmp_path, data_dir):
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok_a", PASS_TOML)
    _mk_unit(sealed, "u_ok_b", PASS_TOML)
    assert exam.run_sealed_exam(sealed_dir=sealed, data_dir=data_dir) is True


def test_uma_falha_false(tmp_path, data_dir):
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok", PASS_TOML)
    _mk_unit(sealed, "u_bad", FAIL_TOML)
    assert exam.run_sealed_exam(sealed_dir=sealed, data_dir=data_dir) is False


def test_excecao_false(tmp_path, data_dir, monkeypatch, capsys):
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok", PASS_TOML)

    def boom(*a, **k):
        raise RuntimeError("explodiu")

    # exam importa run_unit tardiamente, então patch no módulo de origem vale.
    monkeypatch.setattr("harness.graph.run_graph.run_unit", boom)
    assert exam.run_sealed_exam(sealed_dir=sealed, data_dir=data_dir) is False


def test_excecao_na_descoberta_false(data_dir, monkeypatch, capsys):
    def boom(_):
        raise OSError("disco sumiu")

    monkeypatch.setattr(exam, "_discover", boom)
    assert exam.run_sealed_exam(sealed_dir="qualquer", data_dir=data_dir) is False
    assert "fail-closed" in capsys.readouterr().err


def test_report_bem_formado(tmp_path, data_dir):
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_bad", FAIL_TOML)
    _mk_unit(sealed, "u_ok", PASS_TOML)
    report = exam.exam_report(sealed_dir=sealed, data_dir=data_dir)
    assert [r["id"] for r in report] == ["u_bad", "u_ok"]  # ordenado
    assert all(set(r) == {"id", "passed"} for r in report)
    assert [r["passed"] for r in report] == [False, True]
    assert all(isinstance(r["passed"], bool) for r in report)


def test_seed_real_do_repo_passa_com_mock(data_dir):
    """As unidades seedadas em benchmarks/sealed/ passam com o backend mock."""
    sealed = REPO / "benchmarks" / "sealed"
    report = exam.exam_report(sealed_dir=sealed, data_dir=data_dir)
    assert len(report) >= 2
    assert all(r["passed"] for r in report)
