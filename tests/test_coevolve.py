"""Fronteira de dificuldade: passa→fora, falha→dentro, erro→pulado."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.improve import coevolve

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

# Só um executor de verdade resolve: sob o mock a unidade sai do screening.
REAL_TOML = """\
id = "{uid}"
kind = "code"
prompt = "escreva a saída"
verify_cmd = "test -f nao_existe.txt"
requires_real_backend = true
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


def _frontier_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_passa_fica_fora_da_fronteira(tmp_path, data_dir):
    q = tmp_path / "quarantine"
    _mk_unit(q, "q_ok", PASS_TOML)
    frontier_file = tmp_path / "frontier.jsonl"
    assert (
        coevolve.screen_quarantine(
            quarantine_dir=q,
            data_dir=data_dir,
            frontier_path=frontier_file,
            clock=lambda: "2026-08-03T00:00:00+00:00",
        )
        == []
    )
    (row,) = _frontier_rows(frontier_file)
    sec = row.pop("sec")  # duração real, não determinística
    assert isinstance(sec, float) and sec >= 0.0
    assert row == {
        "benchmark": "q_ok",
        "passed": True,
        "timestamp": "2026-08-03T00:00:00+00:00",
        "backend": "mock",
        "model": "",
        "real": False,
    }


def test_falha_entra_na_fronteira(tmp_path, data_dir):
    q = tmp_path / "quarantine"
    _mk_unit(q, "q_bad", FAIL_TOML)
    _mk_unit(q, "q_ok", PASS_TOML)
    frontier_file = tmp_path / "frontier.jsonl"
    assert coevolve.screen_quarantine(
        quarantine_dir=q,
        data_dir=data_dir,
        frontier_path=frontier_file,
        clock=lambda: "T",
    ) == ["q_bad"]
    rows = _frontier_rows(frontier_file)
    assert [(r["benchmark"], r["passed"]) for r in rows] == [
        ("q_bad", False),
        ("q_ok", True),  # ordenado
    ]


def test_erro_ao_rodar_e_pulado(tmp_path, data_dir, monkeypatch, capsys):
    q = tmp_path / "quarantine"
    _mk_unit(q, "q_boom", FAIL_TOML)
    frontier_file = tmp_path / "frontier.jsonl"

    def boom(*a, **k):
        raise RuntimeError("explodiu")

    # coevolve importa run_unit tardiamente: patch no módulo de origem vale.
    monkeypatch.setattr("harness.graph.run_graph.run_unit", boom)
    assert (
        coevolve.screen_quarantine(quarantine_dir=q, data_dir=data_dir, frontier_path=frontier_file)
        == []
    )
    assert "pulado" in capsys.readouterr().err
    assert not frontier_file.exists()  # erro não gera veredito


def test_quarentena_vazia_ou_ausente(tmp_path, data_dir, capsys):
    vazia = tmp_path / "quarantine"
    vazia.mkdir()
    assert coevolve.screen_quarantine(quarantine_dir=vazia, data_dir=data_dir) == []
    ausente = tmp_path / "nao_existe"
    assert coevolve.screen_quarantine(quarantine_dir=ausente, data_dir=data_dir) == []
    assert "não existe" in capsys.readouterr().err


def test_screen_benchmark_tri_state(tmp_path, data_dir):
    q = tmp_path / "quarantine"
    ok = _mk_unit(q, "q_ok", PASS_TOML)
    bad = _mk_unit(q, "q_bad", FAIL_TOML)
    assert coevolve.screen_benchmark(ok, data_dir=data_dir) is True
    assert coevolve.screen_benchmark(bad, data_dir=data_dir) is False
    assert coevolve.screen_benchmark(q / "nao_existe", data_dir=data_dir) is None


def test_mock_pula_unidade_que_exige_backend_real(tmp_path, data_dir, capsys):
    q = tmp_path / "quarantine"
    unit = _mk_unit(q, "q_real", REAL_TOML)
    frontier_file = tmp_path / "frontier.jsonl"

    # Sem screening não há veredito: a unidade não vira fronteira por artefato do mock.
    assert coevolve.screen_benchmark(unit, "mock", "", data_dir) is None
    assert (
        coevolve.screen_quarantine(
            backend="mock", quarantine_dir=q, data_dir=data_dir, frontier_path=frontier_file
        )
        == []
    )
    assert "exige backend real" in capsys.readouterr().err
    assert not frontier_file.exists()


def test_frontier_usa_backend_da_config_e_registra(tmp_path, data_dir, monkeypatch):
    from harness.improve import exam

    cfg = tmp_path / "ruler.toml"
    cfg.write_text('[frontier]\nbackend = "mock2"\nmodel = "m9"\n', encoding="utf-8")
    monkeypatch.setattr(exam, "EXAM_CONFIG", cfg)

    seen: list[tuple] = []

    def fake_run_unit(unit, backend, model, data, thread_id):
        seen.append((backend, model))
        return {"decision": SimpleNamespace(action="accept")}

    monkeypatch.setattr("harness.graph.run_graph.run_unit", fake_run_unit)

    q = tmp_path / "quarantine"
    _mk_unit(q, "q_cfg", FAIL_TOML)
    frontier_file = tmp_path / "frontier.jsonl"
    assert (
        coevolve.screen_quarantine(
            quarantine_dir=q, data_dir=data_dir, frontier_path=frontier_file, clock=lambda: "T"
        )
        == []
    )
    assert seen == [("mock2", "m9")]  # backend do config chegou no run_unit
    (row,) = _frontier_rows(frontier_file)
    assert (row["backend"], row["model"], row["real"]) == ("mock2", "m9", True)


def test_teto_de_unidades_para_o_screening(tmp_path, data_dir, capsys):
    q = tmp_path / "quarantine"
    _mk_unit(q, "q_bad1", FAIL_TOML)
    _mk_unit(q, "q_bad2", FAIL_TOML)
    frontier_file = tmp_path / "frontier.jsonl"
    assert coevolve.screen_quarantine(
        backend="mock",
        quarantine_dir=q,
        data_dir=data_dir,
        frontier_path=frontier_file,
        max_units=1,
    ) == ["q_bad1"]  # o segundo não rodou, logo não é fronteira
    assert "teto de 1 unidades batido" in capsys.readouterr().err


def test_seal_recusa_candidato_fora_da_fronteira(tmp_path, data_dir, monkeypatch, capsys):
    from harness.cli import main
    from harness.improve import synthesize

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(synthesize, "QUARANTINE_DIR", Path("benchmarks/quarantine"))
    monkeypatch.setattr(synthesize, "SEALED_DIR", Path("benchmarks/sealed"))
    _mk_unit(Path("benchmarks/quarantine"), "q_ok", PASS_TOML)

    assert main(["seal", "q_ok", "--yes"]) == 1
    assert "fora da fronteira" in capsys.readouterr().err
    assert Path("benchmarks/quarantine/q_ok/unit.toml").is_file()  # nada movido

    assert main(["seal", "q_ok", "--yes", "--force"]) == 0
    assert Path("benchmarks/sealed/q_ok/unit.toml").is_file()


def test_seal_aceita_candidato_na_fronteira(tmp_path, data_dir, monkeypatch):
    from harness.cli import main
    from harness.improve import synthesize

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(synthesize, "QUARANTINE_DIR", Path("benchmarks/quarantine"))
    monkeypatch.setattr(synthesize, "SEALED_DIR", Path("benchmarks/sealed"))
    _mk_unit(Path("benchmarks/quarantine"), "q_bad", FAIL_TOML)

    assert main(["seal", "q_bad", "--yes"]) == 0
    assert Path("benchmarks/sealed/q_bad/unit.toml").is_file()
