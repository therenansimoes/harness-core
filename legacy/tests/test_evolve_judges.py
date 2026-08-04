#!/usr/bin/env python3
"""Testa `judge_ok` (evolve.py) — espelha `credit_ok`, mas pro sinal de
juízes (SPEC-J1 §7 / SPEC-J2 agregação): mediana_B >= mediana_A - 5,
spread_B <= 25, zero veto de candidato (D2). Sem dados de alguma versão ->
None (FASE 1 é gate manual).

Isolado via monkeypatch em `evolve.JUDGES_VERDICTS_DIR` — nunca lê/escreve
em judges/verdicts/ do repo de verdade.

    python3 -m pytest tests/test_evolve_judges.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import evolve  # noqa: E402


def _write_summary(base: Path, version: str, median, spread, scores=None) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"summary_{version}.json").write_text(
        json.dumps(
            {
                "scores": scores or {"j_b2b": median},
                "median": median,
                "spread": spread,
            }
        )
    )


def _write_verdict(base: Path, judge_id: str, version: str, veto: bool = False) -> None:
    d = base / judge_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{version}.json").write_text(
        json.dumps(
            {
                "judge_id": judge_id,
                "deterministic": {"veto": veto},
            }
        )
    )


def test_judge_ok_aprova(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve, "JUDGES_VERDICTS_DIR", tmp_path)
    _write_summary(tmp_path, "vA", median=80, spread=10)
    _write_summary(tmp_path, "vB", median=82, spread=10)
    assert evolve.judge_ok("vA", "vB") is True


def test_judge_ok_reprova_por_mediana(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve, "JUDGES_VERDICTS_DIR", tmp_path)
    _write_summary(tmp_path, "vA", median=80, spread=10)
    _write_summary(tmp_path, "vB", median=70, spread=10)  # -10, abaixo da margem de -5
    assert evolve.judge_ok("vA", "vB") is False


def test_judge_ok_reprova_por_spread(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve, "JUDGES_VERDICTS_DIR", tmp_path)
    _write_summary(tmp_path, "vA", median=80, spread=10)
    _write_summary(tmp_path, "vB", median=80, spread=30)  # > 25
    assert evolve.judge_ok("vA", "vB") is False


def test_judge_ok_reprova_por_veto_de_candidato(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve, "JUDGES_VERDICTS_DIR", tmp_path)
    _write_summary(tmp_path, "vA", median=80, spread=10)
    _write_summary(tmp_path, "vB", median=85, spread=5)
    _write_verdict(tmp_path, "j_b2b", "vB", veto=True)
    assert evolve.judge_ok("vA", "vB") is False


def test_judge_ok_sem_dados_retorna_none(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve, "JUDGES_VERDICTS_DIR", tmp_path)
    assert evolve.judge_ok("vA", "vB") is None  # nem A nem B tem summary

    _write_summary(tmp_path, "vA", median=80, spread=10)
    assert evolve.judge_ok("vA", "vB") is None  # falta B
