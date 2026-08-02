#!/usr/bin/env python3
"""Testa o consumo do trace.jsonl (SPEC-J2 design 1) do lado de fora do
agent.py: run_task.py (token `trace:<path>` nas notes, GC de runs/) e
run_judge.load_trace (render `"{i}: {line}"` a partir do token).

Sem chamada real ao `claude` — run_task.run_agent é monkeypatchado.

    python3 -m pytest tests/test_trace.py -q
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "judges"))

os.environ.setdefault("PERSONA_MOCK", "1")

import run_task  # noqa: E402
from agent import AgentResult  # noqa: E402

import run_judge  # noqa: E402


# ------------------------------------------------------------- run_task.py


EXPECTED_HEADER = [
    "timestamp",
    "harness_version",
    "backend",
    "model",
    "suite",
    "task_id",
    "success",
    "seconds",
    "tokens",
    "cost_usd",
    "turns",
    "notes",
    # D4a: coleta de KPI por projeto. UMA coluna JSON, não N colunas.
    "kpis",
]


def test_header_results_tsv_byte_a_byte_igual():
    """Aceite 4 do SPEC-J2: o token de trace vai dentro da coluna `notes`
    já existente — o trace não ganha coluna própria. A única coluna posterior
    ao SPEC-J2 é `kpis` (D4a), e é uma só para qualquer número de KPIs."""
    assert run_task.HEADER == EXPECTED_HEADER


def _make_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task_trace_demo"
    task_dir.mkdir()
    (task_dir / "prompt.md").write_text("faça algo")
    (task_dir / "verify.py").write_text("import sys\nsys.exit(0)\n")
    return task_dir


def test_run_once_anexa_token_trace_nas_notes(monkeypatch, tmp_path):
    task_dir = _make_task(tmp_path)
    results_path = tmp_path / "results.tsv"
    monkeypatch.setattr(run_task, "RESULTS", results_path)

    fake_trace = tmp_path / "runs" / "run_abc" / "trace.jsonl"
    fake_trace.parent.mkdir(parents=True)
    fake_trace.write_text('{"type": "result"}\n')

    def fake_run_agent(prompt, ws):
        return AgentResult(
            ok=True, seconds=1.0, tokens=10, cost_usd=0.01, turns=1,
            notes="", trace_path=str(fake_trace), trace_lines=1,
        )

    monkeypatch.setattr(run_task, "run_agent", fake_run_agent)

    ok = run_task.run_once(task_dir, "fixed", keep=False)

    assert ok is True
    rows = results_path.read_text().splitlines()
    assert rows[0].split("\t") == EXPECTED_HEADER
    notes = rows[1].split("\t")[EXPECTED_HEADER.index("notes")]
    assert f"trace:{fake_trace}" in notes


def test_gc_traces_mantem_50_recentes_e_poupa_citados_em_verdict(monkeypatch, tmp_path):
    trace_root = tmp_path / "runs"
    trace_root.mkdir()
    root = tmp_path / "repo"
    (root / "judges" / "verdicts" / "j_b2b").mkdir(parents=True)
    monkeypatch.setattr(run_task, "TRACE_ROOT", trace_root)
    monkeypatch.setattr(run_task, "ROOT", root)

    run_ids = [f"run_{i:03d}" for i in range(55)]
    for i, run_id in enumerate(run_ids):
        d = trace_root / run_id
        d.mkdir()
        (d / "trace.jsonl").write_text("{}\n")
        # mtimes crescentes: run_000 é o mais antigo, run_054 o mais recente.
        t = time.time() - (len(run_ids) - i)
        os.utime(d, (t, t))

    # run_002 estaria fora do top-50 por idade, mas foi citado num verdict.
    cited_id = "run_002"
    (root / "judges" / "verdicts" / "j_b2b" / "v0.2.json").write_text(
        json.dumps({"judge_id": "j_b2b", "trace_run_id": cited_id})
    )

    run_task.gc_traces()

    remaining = {p.name for p in trace_root.iterdir()}
    assert len(remaining) == 51  # 50 mais recentes + 1 poupado por citação
    assert cited_id in remaining
    assert "run_000" not in remaining  # o mais antigo, não citado, foi embora
    assert "run_054" in remaining  # o mais recente sobrevive


# ------------------------------------------------------------ run_judge.py


def test_load_trace_renderiza_com_numero_de_linha(monkeypatch, tmp_path):
    monkeypatch.setattr(run_judge, "REPO_ROOT", tmp_path)
    trace_dir = tmp_path / "runs" / "run_xyz"
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace.jsonl").write_text(
        '{"type": "assistant", "text": "primeiro"}\n'
        '{"type": "result", "result": "DONE: ok"}\n'
    )
    notes = "error_max_turns; trace:runs/run_xyz/trace.jsonl"

    rendered = run_judge.load_trace(notes)

    lines = rendered.splitlines()
    assert lines[0].startswith("1: ")
    assert lines[1].startswith("2: ")
    assert "DONE: ok" in lines[1]


def test_load_trace_fallback_sem_token(monkeypatch, tmp_path):
    monkeypatch.setattr(run_judge, "REPO_ROOT", tmp_path)
    assert run_judge.load_trace("error_max_turns") == "error_max_turns"


def test_load_trace_fallback_arquivo_ausente(monkeypatch, tmp_path):
    monkeypatch.setattr(run_judge, "REPO_ROOT", tmp_path)
    notes = "trace:runs/run_nao_existe/trace.jsonl"
    assert run_judge.load_trace(notes) == notes
