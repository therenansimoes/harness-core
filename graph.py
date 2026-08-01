#!/usr/bin/env python3
"""graph.py — store de auto-crítica do harness em SQLite (stdlib, zero deps).

Guarda o histórico de runs, propostas de evolução do harness e decisões
(merge/discard) tomadas sobre elas. DB default:

    <repo>/evolution/critique.db

Override via env HARNESS_GRAPH ou parâmetro `db_path` opcional em toda
função pública. Schema criado sob demanda (CREATE TABLE IF NOT EXISTS),
idempotente — nunca apaga dados.

Tabelas:
    runs        uma execução de uma task com uma harness_version
    proposals   uma proposta de mudança no harness (from_version -> to_version_intended)
    decisions   o veredito (merge/discard) sobre uma proposal

Elos: runs.proposal_id -> proposals.id ; decisions.proposal_id -> proposals.id
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DEFAULT_DB = ROOT / "evolution" / "critique.db"


def _resolve_db_path(db_path=None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get("HARNESS_GRAPH")
    if env:
        return Path(env)
    return DEFAULT_DB


def _connect(db_path=None) -> sqlite3.Connection:
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            task_id TEXT NOT NULL,
            harness_version TEXT NOT NULL,
            suite TEXT NOT NULL,
            success INTEGER NOT NULL,
            seconds REAL NOT NULL,
            tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            notes TEXT DEFAULT '',
            valid INTEGER NOT NULL DEFAULT 1,
            proposal_id TEXT
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            from_version TEXT NOT NULL,
            to_version_intended TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            diff_summary TEXT NOT NULL,
            path TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            proposal_id TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('merge', 'discard')),
            scores_summary TEXT NOT NULL,
            reason TEXT NOT NULL,
            gates_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_harness_version ON runs(harness_version);
        CREATE INDEX IF NOT EXISTS idx_runs_proposal_id ON runs(proposal_id);
        CREATE INDEX IF NOT EXISTS idx_decisions_proposal_id ON decisions(proposal_id);
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- escrita


def record_run(task_id: str, harness_version: str, suite: str, success: int,
               seconds: float, tokens: int, cost_usd: float, notes: str = "",
               valid: int = 1, proposal_id: str | None = None,
               ts: str | None = None, db_path=None) -> int:
    ts = ts or _now()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO runs
                (ts, task_id, harness_version, suite, success, seconds,
                 tokens, cost_usd, notes, valid, proposal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, task_id, harness_version, suite, success, seconds,
             tokens, cost_usd, notes, valid, proposal_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_proposal(pid: str, from_version: str, to_version_intended: str,
                     hypothesis: str, diff_summary: str, path: str,
                     ts: str | None = None, db_path=None) -> str:
    ts = ts or _now()
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO proposals
                (id, ts, from_version, to_version_intended, hypothesis, diff_summary, path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (pid, ts, from_version, to_version_intended, hypothesis, diff_summary, path),
        )
        conn.commit()
        return pid
    finally:
        conn.close()


def record_decision(proposal_id: str, outcome: str, scores_summary: str,
                     reason: str, gates_json: str, ts: str | None = None,
                     db_path=None) -> int:
    ts = ts or _now()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO decisions
                (ts, proposal_id, outcome, scores_summary, reason, gates_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ts, proposal_id, outcome, scores_summary, reason, gates_json),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ------------------------------------------------------------------- leitura


def recent_decisions(n: int = 10, db_path=None) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.*, p.hypothesis, p.from_version, p.to_version_intended,
                   (SELECT COUNT(*) FROM runs r WHERE r.proposal_id = d.proposal_id) AS n_runs
            FROM decisions d
            LEFT JOIN proposals p ON p.id = d.proposal_id
            ORDER BY d.ts DESC, d.id DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def runs_for_version(version: str, db_path=None) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE harness_version = ?
            ORDER BY ts DESC, id DESC
            """,
            (version,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _summary_one(conn: sqlite3.Connection, version: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM runs WHERE harness_version = ?", (version,)
    ).fetchall()
    n = len(rows)
    if n == 0:
        return {
            "version": version, "n": 0, "n_valid": 0, "success_rate": 0.0,
            "trunc_rate": 0.0, "med_s": 0.0, "cost_run": 0.0, "tok_run": 0.0,
        }

    valid_rows = [r for r in rows if r["valid"]]
    n_valid = len(valid_rows)
    n_trunc = n - n_valid

    success_rate = sum(r["success"] for r in rows) / n
    trunc_rate = n_trunc / n
    med_s = statistics.median([r["seconds"] for r in rows]) if rows else 0.0

    cost_run = (sum(r["cost_usd"] for r in valid_rows) / n_valid) if n_valid else 0.0
    tok_run = (sum(r["tokens"] for r in valid_rows) / n_valid) if n_valid else 0.0

    return {
        "version": version, "n": n, "n_valid": n_valid, "success_rate": success_rate,
        "trunc_rate": trunc_rate, "med_s": med_s, "cost_run": cost_run, "tok_run": tok_run,
    }


def summary_for_ab(version_a: str, version_b: str, db_path=None) -> dict:
    conn = _connect(db_path)
    try:
        return {
            "a": _summary_one(conn, version_a),
            "b": _summary_one(conn, version_b),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- self-test

if __name__ == "__main__":
    import tempfile

    tmp = tempfile.NamedTemporaryFile(prefix="critique_test_", suffix=".db", delete=False)
    tmp.close()
    tmp_db = tmp.name

    try:
        pid = record_proposal(
            pid="p1", from_version="v1", to_version_intended="v2",
            hypothesis="reduzir MAX_TURNS melhora custo sem perder success",
            diff_summary="MAX_TURNS 12 -> 8", path="evolution/proposals/p1",
            db_path=tmp_db,
        )
        assert pid == "p1"

        # 2 runs válidas em v2 ligadas à proposta
        record_run("task_01", "v2", "sealed", success=1, seconds=10.0,
                    tokens=1000, cost_usd=0.10, valid=1, proposal_id="p1", db_path=tmp_db)
        record_run("task_02", "v2", "sealed", success=1, seconds=20.0,
                    tokens=2000, cost_usd=0.20, valid=1, proposal_id="p1", db_path=tmp_db)
        # 1 run inválida (truncada) em v2, também ligada à proposta
        record_run("task_03", "v2", "sealed", success=0, seconds=30.0,
                    tokens=3000, cost_usd=0.30, valid=0, proposal_id="p1", db_path=tmp_db)
        # 1 run de outra versão (v1), não ligada à proposta
        record_run("task_01", "v1", "sealed", success=1, seconds=5.0,
                    tokens=500, cost_usd=0.05, valid=1, db_path=tmp_db)

        record_decision(
            proposal_id="p1", outcome="merge", scores_summary="success +0% cost -10%",
            reason="custo caiu sem perder success", gates_json='{"success_ok": true}',
            db_path=tmp_db,
        )

        decisions = recent_decisions(n=10, db_path=tmp_db)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["proposal_id"] == "p1"
        assert d["outcome"] == "merge"
        assert d["hypothesis"] == "reduzir MAX_TURNS melhora custo sem perder success"
        assert d["from_version"] == "v1"
        assert d["to_version_intended"] == "v2"
        assert d["n_runs"] == 3, f"esperado 3 runs ligadas a p1, veio {d['n_runs']}"

        runs_v2 = runs_for_version("v2", db_path=tmp_db)
        assert len(runs_v2) == 3
        # mais recentes primeiro
        assert runs_v2[0]["task_id"] == "task_03"

        ab = summary_for_ab("v1", "v2", db_path=tmp_db)
        a, b = ab["a"], ab["b"]

        assert a["n"] == 1
        assert a["n_valid"] == 1
        assert a["success_rate"] == 1.0
        assert a["trunc_rate"] == 0.0
        assert a["med_s"] == 5.0
        assert a["cost_run"] == 0.05
        assert a["tok_run"] == 500.0

        assert b["n"] == 3
        assert b["n_valid"] == 2
        # success_rate sobre TODAS as runs: (1+1+0)/3
        assert abs(b["success_rate"] - (2 / 3)) < 1e-9
        # trunc_rate: 1 de 3 é inválida
        assert abs(b["trunc_rate"] - (1 / 3)) < 1e-9
        # mediana de [10, 20, 30] = 20
        assert b["med_s"] == 20.0
        # cost_run/tok_run SÓ sobre as 2 válidas: (0.10+0.20)/2 = 0.15 ; (1000+2000)/2 = 1500
        assert abs(b["cost_run"] - 0.15) < 1e-9, b["cost_run"]
        assert b["tok_run"] == 1500.0, b["tok_run"]

        # versão sem runs -> zeros, sem estourar
        empty = summary_for_ab("nao-existe", "v2", db_path=tmp_db)
        assert empty["a"]["n"] == 0
        assert empty["a"]["success_rate"] == 0.0

        print("OK: todos os asserts de graph.py passaram.")
    finally:
        os.unlink(tmp_db)
