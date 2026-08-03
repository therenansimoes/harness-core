"""MAP-Elites minimalista: um campeão por nicho, persistido em sqlite próprio.

Nicho = (kind, cost_bucket), cost_bucket em {'low','mid','high'}. Não usa o
ledger (harness/ledger) — arquivo separado em data/archive.sqlite.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from harness.ledger.store import data_dir

COST_BUCKETS = ("low", "mid", "high")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS elites (
    kind        TEXT NOT NULL,
    cost_bucket TEXT NOT NULL,
    config      TEXT NOT NULL,
    score       REAL NOT NULL,
    PRIMARY KEY (kind, cost_bucket)
)
"""


def default_path() -> Path:
    return data_dir() / "archive.sqlite"


class Archive:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def add(self, niche: tuple[str, str], config: dict, score: float) -> bool:
        """Registra se for o melhor do nicho. Devolve True se entrou."""
        kind, bucket = niche
        if bucket not in COST_BUCKETS:
            raise ValueError(f"cost_bucket inválido: {bucket!r}")
        cur = self._conn.execute(
            "SELECT score FROM elites WHERE kind=? AND cost_bucket=?", (kind, bucket)
        )
        row = cur.fetchone()
        if row is not None and row[0] >= score:
            return False
        self._conn.execute(
            "INSERT OR REPLACE INTO elites (kind, cost_bucket, config, score) VALUES (?,?,?,?)",
            (kind, bucket, json.dumps(config, sort_keys=True), score),
        )
        self._conn.commit()
        return True

    def best(self, niche: tuple[str, str]) -> tuple[dict, float] | None:
        cur = self._conn.execute(
            "SELECT config, score FROM elites WHERE kind=? AND cost_bucket=?", niche
        )
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0]), row[1]

    def niches(self) -> list[tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT kind, cost_bucket FROM elites ORDER BY kind, cost_bucket"
        )
        return [(k, b) for k, b in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
