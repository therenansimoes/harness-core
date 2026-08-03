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
`record_failure` devolve False e `recall` devolve []. `disabled()` é o mesmo
switch como bloco, pros caminhos de exame/screening.

Arquivamento é SOFT e vive em tabela companheira (`episodic_archived`): FTS5 é
tabela virtual e `ALTER TABLE ... ADD COLUMN` não funciona nela ("virtual tables
may not be altered"), então a flag `archived` não pode ser coluna do índice. O
episódio nunca é deletado — arquivar é esconder do `recall`, e desarquivar é
apagar uma linha da companheira.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from harness.ledger.store import db_path as default_db_path
from harness.ledger.store import now_iso

TABLE = "episodic_failures"
ARCHIVE_TABLE = "episodic_archived"
# Trecho gravado/devolvido: o suficiente para o modelo reconhecer o caso, não a
# trace inteira (que estouraria o system prompt).
MAX_TRACE_CHARS = 800
DEFAULT_K = 3
# Teto de leitura do consolidador: janela de sono não é dump do banco.
DEFAULT_SCAN = 500

SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(
    kind, unit_id, trace, created_at UNINDEXED
);

CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (
    episode_id  INTEGER PRIMARY KEY,
    archived_at TEXT
);
"""

# Subquery do filtro de arquivados: repetida em `recall` e `episodes`, então
# mora num lugar só — divergir os dois faria episódio arquivado voltar a um
# prompt sem ninguém notar.
_NOT_ARCHIVED = f"{TABLE}.rowid NOT IN (SELECT episode_id FROM {ARCHIVE_TABLE})"

_TOKEN = re.compile(r"[0-9A-Za-z_]{3,}")
_OFF = {"0", "off", "false", "no"}
ENV_ENABLED = "HARNESS_EPISODIC"


def _enabled() -> bool:
    """Kill switch lido NA CHAMADA, não no import: teste e operador precisam
    poder desligar a memória sem reimportar o módulo."""
    return os.environ.get(ENV_ENABLED, "1").strip().lower() not in _OFF


@contextmanager
def disabled():
    """Bloco em que a episódica não grava nem lê. Restaura o valor anterior no
    finally, inclusive a ausência da env — quem chama não pode deixar a memória
    desligada pro resto do processo. Reentrante (aninhar é no-op).

    É env var e não flag de argumento porque `record_failure`/`recall` são
    chamados fundo no grafo, longe de quem sabe que o run é uma avaliação.
    """
    previous = os.environ.get(ENV_ENABLED)
    os.environ[ENV_ENABLED] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENV_ENABLED, None)
        else:
            os.environ[ENV_ENABLED] = previous


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Conexão com o índice já criado. Levanta se o sqlite não tem FTS5 —
    quem chama é que decide o fail-open."""
    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Colunas que nasceram depois da tabela companheira. Idempotente: roda a
    cada open, mesmo padrão do `store._migrate` — `CREATE TABLE IF NOT EXISTS`
    não altera tabela existente, então banco de uma versão anterior do
    arquivamento converge por `ALTER TABLE` guardado por `PRAGMA table_info`.
    """
    if _has_column(conn, ARCHIVE_TABLE, "archived_at"):
        return
    conn.execute(f"ALTER TABLE {ARCHIVE_TABLE} ADD COLUMN archived_at TEXT")
    conn.commit()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


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
    Episódio arquivado pelo sono não aparece: foi consolidado ou descartado, e
    devolvê-lo seria o prompt carregando o caso que a consolidação já resolveu.
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
                f"AND {_NOT_ARCHIVED} ORDER BY rank LIMIT ?",
                (match, k),
            ).fetchall()
        return [r["trace"] for r in rows if r["trace"]]
    except Exception:
        return []


@dataclass(frozen=True)
class Episode:
    """Um episódio bruto, como o consolidador precisa dele: `id` é o rowid do
    FTS, que é a chave do arquivamento soft."""

    id: int
    kind: str
    unit_id: str
    trace: str
    created_at: str

    @property
    def timestamp(self) -> datetime | None:
        return parse_ts(self.created_at)


def episodes(
    kind: str | None = None,
    since: datetime | str | None = None,
    limit: int = DEFAULT_SCAN,
    db_path: Path | None = None,
) -> list[Episode]:
    """Episódios NÃO arquivados, mais recentes primeiro. Leitura em bloco para
    o consolidador — o `recall` continua sendo a leitura por relevância.

    `kind=None` é todos os kinds; `since` corta pela data de gravação. Fail-open
    como o resto do módulo: banco sem FTS5 ou erro qualquer devolve [].
    """
    if limit <= 0:
        return []
    where = [_NOT_ARCHIVED]
    params: list[object] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    params.append(limit)
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"SELECT rowid, kind, unit_id, trace, created_at FROM {TABLE} "
                f"WHERE {' AND '.join(where)} ORDER BY rowid DESC LIMIT ?",
                params,
            ).fetchall()
    except Exception:
        return []
    out = [
        Episode(
            id=int(r["rowid"]),
            kind=r["kind"] or "",
            unit_id=r["unit_id"] or "",
            trace=r["trace"] or "",
            created_at=r["created_at"] or "",
        )
        for r in rows
    ]
    # Corte por data em Python, não em SQL: `created_at` é texto ISO com offset e
    # comparar string com um `since` de outro formato daria filtro silenciosamente
    # errado. Episódio com data ilegível fica (o consolidador decide o que fazer).
    floor = parse_ts(_iso(since)) if since is not None else None
    if floor is None:
        return out
    return [e for e in out if (ts := e.timestamp) is None or ts >= floor]


def archive(episode_ids: Iterable[int], db_path: Path | None = None) -> int:
    """Arquiva (soft) os episódios dados; devolve quantos passaram a estar
    arquivados agora. NUNCA deleta: a linha do FTS fica, só sai do `recall`.

    Idempotente por `INSERT OR IGNORE` — dois sonos que escolhem o mesmo órfão
    não podem virar erro no meio de um apply.
    """
    ids = sorted({int(i) for i in episode_ids})
    if not ids:
        return 0
    ts = now_iso()
    try:
        with _connect(db_path) as conn:
            before = _archived_count(conn)
            conn.executemany(
                f"INSERT OR IGNORE INTO {ARCHIVE_TABLE} (episode_id, archived_at) "
                "VALUES (?, ?)",
                [(i, ts) for i in ids],
            )
            return _archived_count(conn) - before
    except Exception:
        return 0


def archived_ids(db_path: Path | None = None) -> list[int]:
    """Ids arquivados, para inspeção e teste. Erro => [] (fail-open)."""
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"SELECT episode_id FROM {ARCHIVE_TABLE} ORDER BY episode_id"
            ).fetchall()
        return [int(r["episode_id"]) for r in rows]
    except Exception:
        return []


def _archived_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {ARCHIVE_TABLE}").fetchone()[0])


def parse_ts(value: str | None) -> datetime | None:
    """ISO do `now_iso` -> datetime aware. Texto ilegível => None: episódio com
    data corrompida não pode derrubar o consolidador."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


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
