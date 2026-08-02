#!/usr/bin/env python3
"""Testa a integração da trilha build (SPEC-J2 design 2, RUBRIC-J2) em
run_judge.py: `--track build`, B1 via accept.py, veto D2 por
accept.py/test_pricing.py forjado no workspace, e ingestão em graph.py com
track/process_json preenchidos.

Sem chamada de API/subprocess `claude` — persona roda em PERSONA_MOCK=1.

    python3 -m pytest tests/test_judges_build.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "judges"))

os.environ["PERSONA_MOCK"] = "1"

import run_judge  # noqa: E402

BUILD_ID = "build_j_b2b"
BUILD_DIR = REPO / "benchmarks" / "judge" / BUILD_ID
SEED_DIR = BUILD_DIR / "seed"

# 7 chaves canônicas de process_metrics.parse_trace (RUBRIC-J2
# §process_metrics.py) + X1/X2/X3 = 10 (aceite 5 do SPEC-J2 design 2).
_EXPECTED_10_KEYS = {
    "n_turns", "n_tool_calls", "n_tool_errors", "n_recovered", "n_thrash",
    "n_help_requests", "stop_reason", "X1", "X2", "X3",
}


def test_all_build_judge_ids_ve_registry_build():
    assert run_judge.all_build_judge_ids() == ["build_j_b2b"]


def test_dry_run_build_gera_verdict_j2_valido_com_10_chaves_de_metrics(tmp_path, monkeypatch):
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    verdict = run_judge.run_dry_build()
    out = run_judge.write_verdict(verdict)

    loaded = json.loads(out.read_text())
    assert loaded["judge_id"] == "build_j_b2b"
    assert loaded["rubric_version"] == "J2"
    assert loaded["track"] == "build"
    assert loaded["build_id"] == "build_j_b2b"
    assert isinstance(loaded["judge_score"], int)
    assert 0 <= loaded["judge_score"] <= 100

    process = loaded["process"]
    assert set(process) == {"X1", "X2", "X3", "metrics"}
    all_keys = set(process["metrics"]) | {"X1", "X2", "X3"}
    assert _EXPECTED_10_KEYS <= all_keys
    assert len(_EXPECTED_10_KEYS) == 10


def test_dry_run_via_cli_imprime_verdict_track_build():
    """subprocess de verdade (não monkeypatchável) -> escreve em
    judges/verdicts/build_j_b2b/ do repo real; limpa no finally pra não
    poluir git status nem os testes de graph_judgements que copiam
    judges/verdicts/ real como fixture."""
    out_dir = REPO / "judges" / "verdicts" / "build_j_b2b"
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "judges" / "run_judge.py"), "--track", "build", "--judge", "build_j_b2b", "--dry-run"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert '"track": "build"' in proc.stdout
        assert '"rubric_version": "J2"' in proc.stdout
        assert "judge_score = " in proc.stdout
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_b1_vermelho_com_seed_intocada():
    """RUBRIC-J2 §B1: contra o seed/ intocado, accept.py é vermelho por
    construção — nada foi implementado ainda."""
    b1_ok, out = run_judge.run_accept_build(BUILD_ID, SEED_DIR)
    assert b1_ok is False
    assert "falhou" in out.lower() or "error" in out.lower()


def test_tamper_accept_py_no_workspace_veto_d2(tmp_path):
    """Agente cria um `accept.py` próprio no workspace (tentativa de
    forjar o verificador) -> build_tampered=True -> D2 veto, B1 zerado,
    judge_score=0, independente do resto."""
    ws = tmp_path / "ws"
    shutil.copytree(SEED_DIR, ws)
    (ws / "accept.py").write_text("print('sempre passa')\n")

    assert run_judge.build_tampered(ws) is True

    deterministic = run_judge.compute_deterministic_build(b1_ok=True, tampered=True, accept_out="")
    assert deterministic["veto"] is True
    assert deterministic["D2"] == 0
    assert deterministic["B1"] == 0

    reg = run_judge.read_registry_build_row(BUILD_ID)
    process = {"X1": 10.0, "X2": run_judge.process_metrics.DISCARDED, "X3": 5.0,
               "metrics": {"n_turns": 1, "n_tool_calls": 0, "n_tool_errors": 0, "n_recovered": 0,
                           "n_thrash": 0, "n_help_requests": 0, "stop_reason": "success"}}
    verdict = run_judge.build_verdict_build(
        BUILD_ID, reg, deterministic, process, persona_scored={}, discarded=[],
        persona_vetoed=False, veto_reason="", cost_usd=0.1,
    )
    assert verdict["judge_score"] == 0
    assert "D2" in verdict["veto_reason"]


def test_test_pricing_no_workspace_tambem_e_tamper(tmp_path):
    """Criar/editar o arquivo de teste selado (test_pricing.py) no
    workspace, mesmo sem accept.py, também é tamper — brief.md proíbe
    editar/apagar arquivos de teste."""
    ws = tmp_path / "ws"
    shutil.copytree(SEED_DIR, ws)
    (ws / "test_pricing.py").write_text("# forjado\n")

    assert run_judge.build_tampered(ws) is True


def test_ingest_verdicts_grava_track_build(tmp_path, monkeypatch):
    verdicts_dir = tmp_path / "verdicts"
    monkeypatch.setattr(run_judge, "VERDICTS_DIR", verdicts_dir)

    verdict = run_judge.run_dry_build()
    run_judge.write_verdict(verdict)

    import graph  # noqa: E402 (import local — evita acoplar módulo no topo do arquivo)

    db_path = tmp_path / "g.db"
    n = graph.ingest_verdicts(verdicts_dir=verdicts_dir, db_path=str(db_path))
    assert n == 1

    conn = graph._connect(str(db_path))
    try:
        row = conn.execute("SELECT track, process_json FROM judgements WHERE judge_id = ?", (BUILD_ID,)).fetchone()
    finally:
        conn.close()
    assert row["track"] == "build"
    process_json = json.loads(row["process_json"])
    assert set(process_json) == {"X1", "X2", "X3", "metrics"}


def test_trilha_a_intocada_run_dry_ainda_funciona():
    """Compatibilidade: run_dry() (trilha A, sem --track) continua com o
    mesmo comportamento de antes desta mudança."""
    verdict = run_judge.run_dry()
    assert verdict["judge_id"] == "j_b2b"
    assert "track" not in verdict
    assert "process" not in verdict
