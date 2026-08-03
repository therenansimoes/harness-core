"""Memória episódica case-based: o que já falhou neste kind, em texto.

Índice FTS5 `episodic_failures` no MESMO `data/runs.sqlite` do ledger — criado
aqui com CREATE VIRTUAL TABLE IF NOT EXISTS, sem tocar em `store.py` (mesma
convenção de `skills/attribution.py`).

FTS5 é opcional no sqlite: build sem a extensão faz `record_failure` virar
no-op silencioso e `recall` devolver []. Toda a API é fail-open — memória que
derruba o run vale menos que memória nenhuma.

O namespace da memória é o data dir GLOBAL (`HARNESS_DATA_DIR`), não o db do
experimento: trocar `HARNESS_DATA_DIR` reseta a memória episódica, e isso é por
design — cada data dir é um harness com sua própria história.

`HARNESS_EPISODIC=0` (ou off/false/no) é kill switch lido a cada chamada:
`record_failure` devolve False e `recall` devolve [].
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from harness.ledger.store import db_path as default_db_path
from harness.ledger.store import now_iso

TABLE = "episodic_failures"
# Trecho gravado/devolvido: o suficiente para o modelo reconhecer o caso, não a
# trace inteira (que estouraria o system prompt).
MAX_TRACE_CHARS = 800
DEFAULT_K = 3

SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(
    kind, unit_id, trace, created_at UNINDEXED
);
"""

_TOKEN = re.compile(r"[0-9A-Za-z_]{3,}")
_OFF = {"0", "off", "false", "no"}


def _enabled() -> bool:
    """Kill switch lido NA CHAMADA, não no import: teste e operador precisam
    poder desligar a memória sem reimportar o módulo."""
    return os.environ.get("HARNESS_EPISODIC", "1").strip().lower() not in _OFF


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Conexão com o índice já criado. Levanta se o sqlite não tem FTS5 —
    quem chama é que decide o fail-open."""
    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_failure(
    kind: str | None,
    unit_id: str,
    trace: str,
    db_path: Path | None = None,
) -> bool:
    """Indexa uma falha. True se gravou; False (silencioso) em qualquer falha.

    Sem `trace` não há caso a lembrar. `kind` vazio virá "" — a busca é sempre
    keyed em kind, então falha sem kind simplesmente nunca é recuperada.
    """
    if not _enabled():
        return False
    if not trace or not trace.strip():
        return False
    try:
        with _connect(db_path) as conn:
            conn.execute(
                f"INSERT INTO {TABLE} (kind, unit_id, trace, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind or "", unit_id or "", trace.strip()[:MAX_TRACE_CHARS], now_iso()),
            )
        return True
    except Exception:
        return False


def recall(
    kind: str | None,
    query: str,
    k: int = DEFAULT_K,
    db_path: Path | None = None,
) -> list[str]:
    """Top-k trechos de falhas passadas do MESMO kind, mais relevantes primeiro.

    `query` é texto livre (o prompt da unidade): vira OR dos seus tokens, porque
    a sintaxe do MATCH não perdoa pontuação. Qualquer erro => [] (fail-open).
    """
    if not _enabled():
        return []
    if not kind or k <= 0:
        return []
    match = _match_expr(kind, query)
    if match is None:
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"SELECT trace FROM {TABLE} WHERE {TABLE} MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, k),
            ).fetchall()
        return [r["trace"] for r in rows if r["trace"]]
    except Exception:
        return []


def _match_expr(kind: str, query: str) -> str | None:
    """`kind:"x" AND (trace:"a" OR trace:"b")`, ou None se não sobrou token.

    Tokens de <3 chars e pontuação saem fora: em FTS5 eles são ruído puro e
    `-`/`:`/`"` no meio do texto viram erro de sintaxe.
    """
    tokens = _TOKEN.findall(query or "")
    if not tokens:
        return None
    # Dedup preservando ordem; teto para não montar expressão gigante.
    seen: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low not in seen:
            seen.append(low)
    terms = " OR ".join(f'trace:"{t}"' for t in seen[:32])
    return f'kind:"{kind}" AND ({terms})'


def render_prompt(traces: list[str]) -> str:
    """Bloco para o system prompt. Lista vazia => "" (nenhum bloco)."""
    if not traces:
        return ""
    items = "\n".join(f"- {t}" for t in traces)
    return (
        "## Falhas passadas semelhantes\n"
        "Runs anteriores do mesmo tipo de tarefa falharam assim. "
        "Não repita o mesmo erro:\n"
        f"{items}"
    )
