#!/usr/bin/env python3
"""Testa a nota humana (D8.1): note.py add/kpi/list.

Nada toca o projects/ real — HARNESS_PROJECTS_ROOT aponta para tmp_path. A
nota é do humano; teste que escrevesse no projeto de verdade viraria KPI falso.

    python3 -m pytest tests/test_note.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import kpi  # noqa: E402
import note  # noqa: E402

RESULTS_HEADER = (
    "timestamp\tharness_version\tbackend\tmodel\tsuite\ttask_id\tsuccess\t"
    "seconds\ttokens\tcost_usd\tturns\tnotes\tkpis"
)
PROJ = "proj"


def _mk_project(tmp_path: Path, monkeypatch, ids=("0001", "0002", "0003", "0004")) -> Path:
    monkeypatch.setenv("HARNESS_PROJECTS_ROOT", str(tmp_path))
    proj = tmp_path / PROJ
    proj.mkdir(parents=True, exist_ok=True)
    lines = [RESULTS_HEADER]
    for i in ids:
        cells = [""] * 13
        cells[0] = f"2026-08-02T10:00:0{len(lines)}+00:00"
        cells[5] = f"{PROJ}/{i}"
        cells[6] = "1"
        lines.append("\t".join(cells))
    (proj / "results.tsv").write_text("\n".join(lines) + "\n")
    return proj


def _add(task_id: str, score: int, why: str = "") -> int:
    return note.main(["add", PROJ, task_id, "--score", str(score), "--why", why])


def test_add_rejects_score_out_of_range(tmp_path, monkeypatch, capsys):
    proj = _mk_project(tmp_path, monkeypatch)
    assert _add("0001", 6) == 2
    assert _add("0001", -1) == 2
    assert not (proj / "notes.tsv").exists(), "nota inválida não pode ser gravada"


def test_add_rejects_unknown_task_id(tmp_path, monkeypatch, capsys):
    proj = _mk_project(tmp_path, monkeypatch)
    assert _add("9999", 4) == 2
    err = capsys.readouterr().err
    # os últimos ids saem no erro: sem isso o humano fica adivinhando o id.
    assert f"{PROJ}/0004" in err
    assert not (proj / "notes.tsv").exists()


def test_add_appends_and_keeps_order(tmp_path, monkeypatch):
    proj = _mk_project(tmp_path, monkeypatch)
    assert _add("0001", 2, "cru") == 0
    assert _add(f"{PROJ}/0002", 5, "bom") == 0
    rows = note.load_notes(PROJ)
    assert [r["task_id"] for r in rows] == [f"{PROJ}/0001", f"{PROJ}/0002"]
    assert [r["score"] for r in rows] == ["2", "5"]
    assert [r["why"] for r in rows] == ["cru", "bom"]
    assert all(r["author"] for r in rows)
    lines = (proj / "notes.tsv").read_text().splitlines()
    assert lines[0].split("\t") == note.NOTES_HEADER
    assert len(lines) == 3


def test_kpi_prints_mean_of_last_window(tmp_path, monkeypatch, capsys):
    _mk_project(tmp_path, monkeypatch, ids=("0001", "0002", "0003", "0004"))
    for i, s in zip(("0001", "0002", "0003", "0004"), (0, 3, 3, 3), strict=False):
        assert _add(i, s) == 0
    capsys.readouterr()
    assert note.main(["kpi", PROJ, "--window", "3"]) == 0
    assert float(capsys.readouterr().out.strip()) == 3.0
    assert note.kpi_value(PROJ, window=3) == 3.0
    assert note.kpi_value(PROJ, window=4) == 2.25


def test_kpi_exits_1_with_fewer_than_three_notes(tmp_path, monkeypatch, capsys):
    _mk_project(tmp_path, monkeypatch)
    capsys.readouterr()
    # nenhuma nota
    assert note.main(["kpi", PROJ]) == 1
    assert capsys.readouterr().out == ""
    for i, s in zip(("0001", "0002"), (5, 5), strict=False):
        assert _add(i, s) == 0
    capsys.readouterr()
    # 2 notas ainda é opinião solta: 1 nota virando média reverteria versão.
    assert note.main(["kpi", PROJ]) == 1
    assert capsys.readouterr().out == ""
    assert _add("0003", 5) == 0
    capsys.readouterr()
    assert note.main(["kpi", PROJ]) == 0
    assert capsys.readouterr().out.strip() == "5.0"


def test_kpi_stdout_last_line_parses_with_kpi_parse_value(tmp_path, monkeypatch, capsys):
    _mk_project(tmp_path, monkeypatch)
    for i, s in zip(("0001", "0002", "0003"), (4, 5, 3), strict=False):
        assert _add(i, s) == 0
    capsys.readouterr()
    assert note.main(["kpi", PROJ]) == 0
    assert kpi.parse_value(capsys.readouterr().out) == pytest.approx(4.0)


def test_kpi_via_subprocess_com_harness_root(tmp_path, monkeypatch):
    """O caminho de verdade: kpi.run_kpi roda o cmd do kpi.toml com cwd no alvo
    e $HARNESS_ROOT apontando para a raiz do harness."""
    _mk_project(tmp_path, monkeypatch)
    for i, s in zip(("0001", "0002", "0003"), (4, 5, 3), strict=False):
        assert _add(i, s) == 0
    alvo = tmp_path / "ws"
    alvo.mkdir()
    v = kpi.run_kpi(f'python3 "$HARNESS_ROOT/note.py" kpi {PROJ}', alvo)
    assert v == pytest.approx(4.0)
