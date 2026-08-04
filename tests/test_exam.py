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


# --------------------------------------------------------------- backend do exame

REAL_TOML = PASS_TOML + "requires_real_backend = true\n"


def _spy_run_unit(monkeypatch) -> list[tuple]:
    """Grava (unit_dir, backend, model) e devolve decisão de accept."""
    calls: list[tuple] = []

    class _Decision:
        action = "accept"

    def fake(unit_dir, backend, model, data, thread_id, *a, **k):
        calls.append((Path(unit_dir).name, backend, model))
        return {"decision": _Decision()}

    monkeypatch.setattr("harness.graph.run_graph.run_unit", fake)
    return calls


def test_sem_config_extra_usa_mock(tmp_path, data_dir, monkeypatch):
    """Default = comportamento histórico: mock, model vazio (vira None)."""
    calls = _spy_run_unit(monkeypatch)
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok", PASS_TOML)
    assert exam.run_sealed_exam(sealed_dir=sealed, data_dir=data_dir) is True
    assert calls == [("u_ok", "mock", None)]


def test_config_exam_roteia_backend_real(tmp_path, data_dir, monkeypatch):
    cfg = tmp_path / "ruler.toml"
    cfg.write_text(
        '[exam]\nbackend = "deepagents"\nmodel = "openai:qwen3.5-9b-mlx"\n', encoding="utf-8"
    )
    calls = _spy_run_unit(monkeypatch)
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok", PASS_TOML)
    _mk_unit(sealed, "u_real", REAL_TOML)
    assert (
        exam.run_sealed_exam(sealed_dir=sealed, data_dir=data_dir, config_path=cfg) is True
    )
    # backend real: NENHUMA unidade fica fora, e todas vão pro backend do config.
    assert calls == [
        ("u_ok", "deepagents", "openai:qwen3.5-9b-mlx"),
        ("u_real", "deepagents", "openai:qwen3.5-9b-mlx"),
    ]


def test_arg_explicito_vence_config(tmp_path, data_dir, monkeypatch):
    cfg = tmp_path / "ruler.toml"
    cfg.write_text('[exam]\nbackend = "deepagents"\nmodel = "x"\n', encoding="utf-8")
    calls = _spy_run_unit(monkeypatch)
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok", PASS_TOML)
    exam.exam_report(backend="mock", sealed_dir=sealed, data_dir=data_dir, config_path=cfg)
    assert calls == [("u_ok", "mock", "x")]


def test_config_torto_degrada_para_mock(tmp_path):
    assert exam.exam_backend(tmp_path / "nao_existe.toml") == ("mock", "")
    torto = tmp_path / "torto.toml"
    torto.write_text("[exam]\nbackend = 7\n", encoding="utf-8")
    assert exam.exam_backend(torto) == ("mock", "")
    assert exam.exam_backend(REPO / "config" / "ruler.toml") == ("mock", "")


def test_unidade_requires_real_fica_fora_do_exame_mock(tmp_path, data_dir, capsys):
    sealed = tmp_path / "sealed"
    _mk_unit(sealed, "u_ok", PASS_TOML)
    # verify que o mock nunca satisfaz + requires_real_backend => fora, não reprova.
    _mk_unit(sealed, "u_real", FAIL_TOML + "requires_real_backend = true\n")
    report = exam.exam_report(sealed_dir=sealed, data_dir=data_dir)
    assert [r["id"] for r in report] == ["u_ok"]
    assert "u_real exige backend real" in capsys.readouterr().err


def test_tasks_do_repo_exigem_backend_real(data_dir, capsys):
    """task_s01/s02 têm unit.toml e ficam fora só do exame mock."""
    sealed = REPO / "benchmarks" / "sealed"
    ids = [r["id"] for r in exam.exam_report(sealed_dir=sealed, data_dir=data_dir)]
    assert "task_s01" not in ids and "task_s02" not in ids
    for name in ("task_s01", "task_s02"):
        assert exam._requires_real_backend(sealed / name) is True
