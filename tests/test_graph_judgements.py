#!/usr/bin/env python3
"""Testa a tabela `judgements` do graph.py (SPEC-J2.md §Agregação, SPEC-J1.md §7).

Ingestão dos verdicts reais do repo (copiados como fixture), idempotência, e
schema antigo intacto. DB do graph é temporário, nada aqui chama API nem rede.

    python3 -m pytest tests/test_graph_judgements.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="graph_judgements_test_"))

os.environ["HARNESS_GRAPH"] = str(TMP / "critique.db")
sys.path.insert(0, str(REPO))

import graph  # noqa: E402

REAL_VERDICTS_DIR = REPO / "attic" / "judges" / "verdicts"


@pytest.fixture(scope="module", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture()
def fixtures_dir() -> Path:
    """Cópia dos verdicts reais do repo, isolada em /tmp (nunca escreve no repo)."""
    dst = TMP / f"verdicts_{next(_counter)}"
    shutil.copytree(REAL_VERDICTS_DIR, dst)
    return dst


def _counter_gen():
    i = 0
    while True:
        yield i
        i += 1


_counter = _counter_gen()


def _fresh_db() -> str:
    db = TMP / f"db_{next(_counter)}.db"
    return str(db)


def test_ingest_verdicts_reais(fixtures_dir):
    db_path = _fresh_db()
    n = graph.ingest_verdicts(verdicts_dir=fixtures_dir, db_path=db_path)

    # Conta esperada derivada da própria fixture (judges/verdicts/ é diretório
    # VIVO — novos juízes/tracks entram; hardcode aqui quebra a cada verdict novo).
    # Mesmo glob de ingest_verdicts: <judge>/<arquivo>.json, sem summary_*.
    expected = len(
        [p for p in fixtures_dir.glob("*/*.json") if not p.name.startswith("summary_")]
    )
    assert n == expected

    rows = graph.judge_history(n=100, db_path=db_path)
    assert len(rows) == expected

    by_key = {(r["judge_id"], r["harness_version"]): r for r in rows}
    assert ("j_b2b", "v0.2") in by_key
    assert ("j_b2b", "v0.4") in by_key
    assert ("j_hw", "v0.4") in by_key
    assert ("j_web", "v0.4") in by_key

    v04 = by_key[("j_b2b", "v0.4")]
    v04_fixture_file = fixtures_dir / "j_b2b" / "v0.4.json"
    with open(v04_fixture_file) as f:
        v04_expected = json.load(f)
    assert v04["judge_score"] == v04_expected["judge_score"]
    assert v04["rubric_version"] == "J1"
    assert v04["veto"] == 0
    assert v04["persona_vetoed"] == 0
    assert v04["track"] is None
    assert v04["process_json"] is None

    det = json.loads(v04["deterministic_json"])
    expected_det = v04_expected.get("deterministic", {})
    assert det["D1"] == expected_det.get("D1")
    persona = json.loads(v04["persona_json"])
    expected_persona = v04_expected.get("persona", {})
    assert persona == expected_persona

    v02 = by_key[("j_b2b", "v0.2")]
    v02_fixture_file = fixtures_dir / "j_b2b" / "v0.2.json"
    with open(v02_fixture_file) as f:
        v02_expected = json.load(f)
    assert v02["judge_score"] == v02_expected["judge_score"]
    assert v02["persona_json"] == "{}"


def test_ingest_verdicts_ignora_summary(fixtures_dir):
    db_path = _fresh_db()
    # summary_v0.4.json está solto em verdicts/, fora de qualquer subpasta
    # <judge_id>/ — não bate no glob */*.json e não vira linha.
    assert (fixtures_dir / "summary_v0.4.json").exists()
    graph.ingest_verdicts(verdicts_dir=fixtures_dir, db_path=db_path)
    rows = graph.judge_history(n=100, db_path=db_path)
    assert all(r["judge_id"] != "summary" for r in rows)


def test_ingest_verdicts_idempotente(fixtures_dir):
    db_path = _fresh_db()
    expected = len(
        [p for p in fixtures_dir.glob("*/*.json") if not p.name.startswith("summary_")]
    )
    n1 = graph.ingest_verdicts(verdicts_dir=fixtures_dir, db_path=db_path)
    n2 = graph.ingest_verdicts(verdicts_dir=fixtures_dir, db_path=db_path)
    assert n1 == expected
    assert n2 == expected  # varre de novo, mas não duplica linha

    rows = graph.judge_history(n=100, db_path=db_path)
    assert len(rows) == expected  # upsert, não insert cego

    # rodar uma terceira vez continua estável
    graph.ingest_verdicts(verdicts_dir=fixtures_dir, db_path=db_path)
    rows_again = graph.judge_history(n=100, db_path=db_path)
    assert len(rows_again) == expected


def test_ingest_verdicts_dir_vazio_ou_ausente():
    db_path = _fresh_db()
    n = graph.ingest_verdicts(verdicts_dir=TMP / "nao-existe", db_path=db_path)
    assert n == 0
    assert graph.judge_history(db_path=db_path) == []


def test_record_judgement_upsert_direto():
    """record_judgement isolado: mesma chave (judge_id, harness_version,
    rubric_version, ts) atualiza a linha em vez de duplicar."""
    db_path = _fresh_db()
    ts = "2026-08-02T00:00:00+00:00"
    id1 = graph.record_judgement(
        judge_id="j_b2b", harness_version="v9.9", rubric_version="J1",
        judge_score=10, deterministic_json="{}", persona_json="{}",
        veto=0, persona_vetoed=0, ts=ts, db_path=db_path,
    )
    id2 = graph.record_judgement(
        judge_id="j_b2b", harness_version="v9.9", rubric_version="J1",
        judge_score=20, deterministic_json="{}", persona_json="{}",
        veto=1, persona_vetoed=0, ts=ts, db_path=db_path,
    )
    assert id1 == id2  # mesma linha, atualizada

    rows = graph.judge_history(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["judge_score"] == 20
    assert rows[0]["veto"] == 1


def test_schema_antigo_intacto(fixtures_dir):
    """A tabela judgements é puramente aditiva: runs/proposals/decisions e o
    resto do schema continuam funcionando com dados, sem migração."""
    db_path = _fresh_db()

    graph.record_proposal(
        pid="pj1", from_version="v1", to_version_intended="v2",
        hypothesis="teste de convivência com judgements", diff_summary="nada",
        path="evolution/proposals/pj1", db_path=db_path,
    )
    graph.record_run("task_01", "v2", "sealed", success=1, seconds=1.0,
                      tokens=100, cost_usd=0.01, proposal_id="pj1", db_path=db_path)
    graph.record_decision(
        proposal_id="pj1", outcome="merge", scores_summary="ok",
        reason="ok", gates_json="{}", db_path=db_path,
    )

    # ingesta judgements no MESMO db
    graph.ingest_verdicts(verdicts_dir=fixtures_dir, db_path=db_path)

    decisions = graph.recent_decisions(n=10, db_path=db_path)
    assert len(decisions) == 1
    assert decisions[0]["proposal_id"] == "pj1"
    assert decisions[0]["n_runs"] == 1

    runs = graph.runs_for_version("v2", db_path=db_path)
    assert len(runs) == 1
    assert runs[0]["task_id"] == "task_01"

    # e o eixo harness continua isolado do eixo judgements
    assert all("judge_id" not in r for r in runs)

    rows = graph.judge_history(n=100, db_path=db_path)
    assert len(rows) >= 4  # judgements ingeridos seguem visíveis (dir vivo, conta cresce)
