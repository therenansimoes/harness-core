import tomllib
from pathlib import Path

from harness import cli
from harness.improve import synthesize
from harness.improve.synthesize import synthesize_from_failures
from harness.types import RunRow


def _row(unit_id: str, ok: bool, exit_reason: str, run_id: str = "r1") -> RunRow:
    return RunRow(
        run_id=run_id, unit_id=unit_id, project=None, backend="mock",
        model=None, tier=None, kind="code", ok=ok, exit_reason=exit_reason,
        sec_total=1.0, sec_provision=0.1, cost_usd=None, intervention=False,
        created_at="2026-08-03T00:00:00+00:00",
    )


def _unit(units_dir: Path, unit_id: str) -> None:
    d = units_dir / unit_id
    d.mkdir(parents=True)
    (d / "unit.toml").write_text(
        f'id = "{unit_id}"\nkind = "code"\n'
        f'prompt = "Conserte o {unit_id}."\nverify_cmd = "true"\n',
        encoding="utf-8",
    )


def test_synthesize_gera_unit_toml_parseavel(tmp_path):
    units = tmp_path / "held_in"
    out = tmp_path / "quarantine"
    _unit(units, "u1")
    created = synthesize_from_failures(
        [_row("u1", ok=False, exit_reason="verify_failed", run_id="run-77")],
        out_dir=out, units_dir=units,
    )
    assert created == [out / "u1"]
    data = tomllib.loads((out / "u1" / "unit.toml").read_text(encoding="utf-8"))
    assert data["id"] == "u1"
    assert data["prompt"] == "Conserte o u1."
    assert data["verify_cmd"] == "true"
    assert data["origin"] == {"run_id": "run-77", "exit_reason": "verify_failed"}
    # o exame sintetizado é carregável pelo mesmo loader do `harness run`
    assert cli.load_unit(out / "u1").id == "u1"


def test_synthesize_ignora_sucesso_e_pega_revertido(tmp_path):
    units = tmp_path / "held_in"
    out = tmp_path / "quarantine"
    _unit(units, "ok_unit")
    _unit(units, "rev_unit")
    created = synthesize_from_failures(
        [
            _row("ok_unit", ok=True, exit_reason="done"),
            _row("rev_unit", ok=True, exit_reason="reverted"),
        ],
        out_dir=out, units_dir=units,
    )
    assert created == [out / "rev_unit"]


def test_synthesize_dedupe_por_unit_id(tmp_path):
    units = tmp_path / "held_in"
    out = tmp_path / "quarantine"
    _unit(units, "u1")
    rows = [
        _row("u1", ok=False, exit_reason="verify_failed", run_id="a"),
        _row("u1", ok=False, exit_reason="timeout", run_id="b"),
    ]
    assert len(synthesize_from_failures(rows, out_dir=out, units_dir=units)) == 1
    # segunda passada: dir já existe, nada regenerado
    assert synthesize_from_failures(rows, out_dir=out, units_dir=units) == []


def test_synthesize_pula_unidade_sem_original(tmp_path):
    out = tmp_path / "quarantine"
    created = synthesize_from_failures(
        [_row("fantasma", ok=False, exit_reason="verify_failed")],
        out_dir=out, units_dir=tmp_path / "held_in",
    )
    assert created == []
    assert not (out / "fantasma").exists()


def _quarantine(tmp_path, monkeypatch, name: str = "u1") -> tuple[Path, Path]:
    q = tmp_path / "quarantine"
    s = tmp_path / "sealed"
    (q / name).mkdir(parents=True)
    # verify_cmd que falha: o candidato está na fronteira (harness atual não
    # passa), então `seal` não o recusa por "fora da fronteira".
    (q / name / "unit.toml").write_text(
        'id = "u1"\nprompt = "p"\nverify_cmd = "false"\n', encoding="utf-8"
    )
    monkeypatch.setattr(synthesize, "QUARANTINE_DIR", q)
    monkeypatch.setattr(synthesize, "SEALED_DIR", s)
    return q, s


def test_seal_move_com_yes(tmp_path, monkeypatch):
    q, s = _quarantine(tmp_path, monkeypatch)
    assert cli.main(["seal", "u1", "--yes"]) == 0
    assert not (q / "u1").exists()
    assert (s / "u1" / "unit.toml").is_file()


def test_seal_recusa_sem_yes(tmp_path, monkeypatch, capsys):
    q, s = _quarantine(tmp_path, monkeypatch)
    assert cli.main(["seal", "u1"]) == 1
    assert (q / "u1").exists()
    assert not (s / "u1").exists()
    assert "ato humano" in capsys.readouterr().err


def test_seal_nome_inexistente(tmp_path, monkeypatch, capsys):
    _quarantine(tmp_path, monkeypatch)
    assert cli.main(["seal", "nao-existe", "--yes"]) == 1
    assert "não existe" in capsys.readouterr().err
