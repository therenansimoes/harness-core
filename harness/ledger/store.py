"""Ledger de runs em SQLite.

Caminho do banco: `$HARNESS_DATA_DIR/runs.sqlite`, default `data/runs.sqlite`
relativo ao cwd. O env var existe para o teste apontar para um tmpdir.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from harness.types import RunRow

DB_NAME = "runs.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    unit_id       TEXT    NOT NULL,
    project       TEXT,
    backend       TEXT    NOT NULL,
    model         TEXT,
    tier          TEXT,
    kind          TEXT,
    ok            INTEGER NOT NULL,
    exit_reason   TEXT    NOT NULL,
    sec_total     REAL    NOT NULL,
    sec_provision REAL    NOT NULL,
    cost_usd      REAL,
    intervention  INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_run_id ON runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_prior ON runs(kind, tier, backend);
"""

_COLUMNS = (
    "run_id", "unit_id", "project", "backend", "model", "tier", "kind",
    "ok", "exit_reason", "sec_total", "sec_provision", "cost_usd",
    "intervention", "created_at",
)


def data_dir() -> Path:
    return Path(os.environ.get("HARNESS_DATA_DIR", "data"))


def db_path() -> Path:
    return data_dir() / DB_NAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Abre (e cria) o banco, garantindo diretório e schema."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_run(row: RunRow, path: Path | None = None) -> int:
    """Grava uma linha e devolve o rowid."""
    values = [_encode(getattr(row, c)) for c in _COLUMNS]
    placeholders = ", ".join("?" * len(_COLUMNS))
    with connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO runs ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        return int(cur.lastrowid)


def history(
    project: str | None = None,
    kind: str | None = None,
    backend: str | None = None,
    limit: int = 500,
    path: Path | None = None,
) -> list[RunRow]:
    """Runs mais recentes primeiro, filtradas pelas chaves do prior."""
    where: list[str] = []
    params: list[object] = []
    for col, val in (("project", project), ("kind", kind), ("backend", backend)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM runs{clause} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
    return [_row(r) for r in rows]


def _encode(value: object) -> object:
    return int(value) if isinstance(value, bool) else value


def _row(r: sqlite3.Row) -> RunRow:
    return RunRow(
        id=r["id"],
        run_id=r["run_id"],
        unit_id=r["unit_id"],
        project=r["project"],
        backend=r["backend"],
        model=r["model"],
        tier=r["tier"],
        kind=r["kind"],
        ok=bool(r["ok"]),
        exit_reason=r["exit_reason"],
        sec_total=r["sec_total"],
        sec_provision=r["sec_provision"],
        cost_usd=r["cost_usd"],
        intervention=bool(r["intervention"]),
        created_at=r["created_at"],
    )
