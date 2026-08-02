#!/usr/bin/env python3
"""Testa a coleta de KPI por projeto (D4a): kpi.py + coluna `kpis` no
results.tsv.

Nenhuma chamada real ao `claude` — run_task.run_agent é monkeypatchado.

    python3 -m pytest tests/test_kpi.py -q
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("PERSONA_MOCK", "1")

import graph  # noqa: E402
import kpi  # noqa: E402
import run_task  # noqa: E402
import score  # noqa: E402
from agent import AgentResult  # noqa: E402


def _write_kpi_toml(repo: Path, body: str) -> None:
    (repo / ".harness").mkdir(parents=True, exist_ok=True)
    (repo / ".harness" / "kpi.toml").write_text(body)


# -------------------------------------------------------------------- kpi.py


def test_toml_valido_coleta_valores(tmp_path):
    (tmp_path / "src.txt").write_text("a\nb\nc\n")
    _write_kpi_toml(
        tmp_path,
        '[kpi.linhas]\ncmd = "wc -l < src.txt"\n\n'
        '[kpi.constante]\ncmd = "echo 42.5"\ntimeout_s = 5\n',
    )

    assert kpi.collect(tmp_path) == {"linhas": 3.0, "constante": 42.5}


def test_ultima_linha_e_a_que_vale(tmp_path):
    _write_kpi_toml(tmp_path, '[kpi.x]\ncmd = "echo ruido; echo 7"\n')
    assert kpi.collect(tmp_path) == {"x": 7.0}


def test_cmd_que_falha_vira_nan(tmp_path):
    _write_kpi_toml(
        tmp_path,
        '[kpi.exit_1]\ncmd = "echo 9; exit 1"\n\n'
        '[kpi.nao_numero]\ncmd = "echo tudo certo"\n\n'
        '[kpi.vazio]\ncmd = "true"\n',
    )

    vals = kpi.collect(tmp_path)
    assert set(vals) == {"exit_1", "nao_numero", "vazio"}
    assert all(math.isnan(v) for v in vals.values())


def test_sem_kpi_toml_devolve_vazio(tmp_path):
    assert kpi.load_kpis(tmp_path) == {}
    assert kpi.collect(tmp_path) == {}
    assert kpi.to_json({}) == "{}"


def test_toml_invalido_nao_explode(tmp_path):
    _write_kpi_toml(tmp_path, "[kpi.x\ncmd = ")
    assert kpi.collect(tmp_path) == {}


def test_entrada_sem_cmd_e_ignorada(tmp_path):
    _write_kpi_toml(tmp_path, '[kpi.sem_cmd]\ntimeout_s = 3\n\n[kpi.ok]\ncmd = "echo 1"\n')
    assert kpi.collect(tmp_path) == {"ok": 1.0}


def test_timeout_respeitado(tmp_path):
    """`sleep` além do timeout_s vira nan e a coleta volta rápido — o teto é
    do timeout declarado, não do comando."""
    _write_kpi_toml(tmp_path, '[kpi.lento]\ncmd = "sleep 30; echo 1"\ntimeout_s = 1\n')

    t0 = time.monotonic()
    vals = kpi.collect(tmp_path)
    elapsed = time.monotonic() - t0

    assert math.isnan(vals["lento"])
    assert elapsed < 10, f"timeout não cortou o sleep (levou {elapsed:.1f}s)"


def test_timeout_default_60(tmp_path):
    _write_kpi_toml(tmp_path, '[kpi.a]\ncmd = "echo 1"\n')
    assert kpi.load_kpis(tmp_path)["a"]["timeout_s"] == kpi.DEFAULT_TIMEOUT_S == 60


def test_cmd_roda_no_workspace_do_alvo(tmp_path):
    """cwd é o repo passado, não o cwd do harness."""
    _write_kpi_toml(tmp_path, '[kpi.marcador]\ncmd = "cat marcador"\n')
    (tmp_path / "marcador").write_text("123\n")
    assert kpi.collect(tmp_path) == {"marcador": 123.0}


def test_run_kpi_exports_harness_root(tmp_path):
    """cwd é o alvo, então um KPI que usa ferramenta do harness (note.py) só
    acha a raiz por $HARNESS_ROOT."""
    assert kpi.run_kpi('test -f "$HARNESS_ROOT/kpi.py" && echo 1', tmp_path) == 1.0
    _write_kpi_toml(tmp_path, '[kpi.raiz]\ncmd = "echo $HARNESS_ROOT | wc -c"\n')
    assert kpi.collect(tmp_path)["raiz"] == float(len(str(kpi.ROOT)) + 1)
    assert kpi.ROOT == REPO


def test_to_json_compacto_sem_tab_nem_quebra(tmp_path):
    s = kpi.to_json({"b": 2.0, "a": 1.0})
    assert s == '{"a":1.0,"b":2.0}'
    assert "\t" not in s and "\n" not in s
    assert json.loads(s) == {"a": 1.0, "b": 2.0}


def test_cli_imprime_o_dict(tmp_path):
    (tmp_path / "src.txt").write_text("a\nb\n")
    _write_kpi_toml(tmp_path, '[kpi.linhas]\ncmd = "wc -l < src.txt"\n')

    p = subprocess.run(
        [sys.executable, str(REPO / "kpi.py"), str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.strip()) == {"linhas": 2.0}


# ---------------------------------------------------------------- run_task.py


def _make_task(tmp_path: Path, with_kpi: bool) -> Path:
    task_dir = tmp_path / "task_kpi_demo"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "prompt.md").write_text("faça algo")
    (task_dir / "verify.py").write_text("import sys\nsys.exit(0)\n")
    (task_dir / "fixtures" / "src.txt").write_text("a\nb\nc\nd\n")
    if with_kpi:
        _write_kpi_toml(task_dir / "fixtures", '[kpi.linhas]\ncmd = "wc -l < src.txt"\n')
    return task_dir


def _patch_agent(monkeypatch):
    def fake_run_agent(prompt, ws):
        return AgentResult(
            ok=True, seconds=1.0, tokens=10, cost_usd=0.01, turns=1,
            notes="", trace_path="", trace_lines=0,
        )

    monkeypatch.setattr(run_task, "run_agent", fake_run_agent)


def _rows(path: Path) -> list[dict]:
    lines = path.read_text().splitlines()
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def test_run_grava_coluna_kpis(monkeypatch, tmp_path):
    task_dir = _make_task(tmp_path, with_kpi=True)
    results = tmp_path / "results.tsv"
    monkeypatch.setattr(run_task, "RESULTS", results)
    _patch_agent(monkeypatch)

    assert run_task.run_once(task_dir, "fixed", keep=False) is True

    row = _rows(results)[0]
    assert json.loads(row["kpis"]) == {"linhas": 4.0}


def test_run_sem_kpi_toml_nao_quebra(monkeypatch, tmp_path):
    task_dir = _make_task(tmp_path, with_kpi=False)
    results = tmp_path / "results.tsv"
    monkeypatch.setattr(run_task, "RESULTS", results)
    _patch_agent(monkeypatch)

    assert run_task.run_once(task_dir, "fixed", keep=False) is True
    assert _rows(results)[0]["kpis"] == "{}"


def test_linha_antiga_sem_coluna_kpis_ainda_parseia(tmp_path, monkeypatch):
    """results.tsv gravado antes do D4a: header de 12 colunas, sem `kpis`.
    Quem lê (score.load) precisa continuar parseando com default vazio."""
    old_header = [c for c in run_task.HEADER if c != "kpis"]
    old_line = [
        "2026-08-01T12:00:00+00:00", "v0.4", "cli", "claude-haiku-4-5", "fixed",
        "task_01", "1", "12.3", "1000", "0.0100", "3", "",
    ]
    path = tmp_path / "results.tsv"
    path.write_text("\t".join(old_header) + "\n" + "\t".join(old_line) + "\n")

    monkeypatch.setattr(score, "RESULTS", path)
    rows = score.load()

    assert len(rows) == 1
    assert rows[0]["task_id"] == "task_01"
    assert rows[0].get("kpis", "") == ""
    assert score.agg(rows)["pass"] == 1


# ------------------------------------------------------------------- graph.py


def test_graph_guarda_kpis(tmp_path):
    db = tmp_path / "critique.db"
    graph.record_run("task_01", "v0.4", "fixed", success=1, seconds=1.0, tokens=10,
                     cost_usd=0.01, kpis='{"linhas":4.0}', db_path=db)

    rows = graph.runs_for_version("v0.4", db_path=db)
    assert json.loads(rows[0]["kpis"]) == {"linhas": 4.0}


def test_graph_migra_db_antigo_sem_coluna_kpis(tmp_path):
    """DB criado antes do D4a: a tabela runs já existe, então CREATE TABLE IF
    NOT EXISTS não acrescenta nada — o ALTER TABLE guardado é que migra, sem
    perder a linha que já estava lá."""
    db = tmp_path / "velho.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, task_id TEXT NOT NULL,
            harness_version TEXT NOT NULL, suite TEXT NOT NULL,
            success INTEGER NOT NULL, seconds REAL NOT NULL,
            tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
            notes TEXT DEFAULT '', valid INTEGER NOT NULL DEFAULT 1,
            proposal_id TEXT
        );
        INSERT INTO runs (ts, task_id, harness_version, suite, success, seconds, tokens, cost_usd)
        VALUES ('2026-08-01T00:00:00+00:00', 'task_00', 'v0.3', 'fixed', 1, 2.0, 5, 0.005);
        """
    )
    conn.commit()
    conn.close()

    graph.record_run("task_01", "v0.3", "fixed", success=1, seconds=1.0, tokens=10,
                     cost_usd=0.01, kpis='{"a":1.0}', db_path=db)

    rows = {r["task_id"]: r for r in graph.runs_for_version("v0.3", db_path=db)}
    assert set(rows) == {"task_00", "task_01"}
    assert rows["task_00"]["kpis"] in ("", None)   # linha pré-migração
    assert json.loads(rows["task_01"]["kpis"]) == {"a": 1.0}
