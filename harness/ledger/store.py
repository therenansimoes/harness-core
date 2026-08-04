"""Ledger de runs em SQLite.

Caminho do banco: `$HARNESS_DATA_DIR/runs.sqlite`, default `data/runs.sqlite`
relativo ao cwd. O env var existe para o teste apontar para um tmpdir.

Além das runs, guarda `node_events`: marca `(run_id, node, attempt)` de toda
escrita externa feita por um nó do grafo. É o que torna o resume idempotente.
O `attempt` fica em 0 para os nós que rodam uma vez por run (plan, provision,
record); os nós por-tentativa (execute, verify) passam o attempt corrente, senão
o retry replaya o resultado da tentativa anterior em vez de reexecutar.

`mutations` é a terceira tabela: uma linha por mutação de config que o loop de
melhoria avaliou (PR-9). Não é derivável de `runs` — a mesma amostra de runs
podia ter sido gerada sem experimento nenhum. É o que o replay do PR-10 lê para
atribuir delta a uma mudança.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from harness.types import MutationRow, RunRow

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
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    intervention  INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_run_id ON runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_prior ON runs(kind, tier, backend);

CREATE TABLE IF NOT EXISTS node_events (
    run_id     TEXT    NOT NULL,
    node       TEXT    NOT NULL,
    attempt    INTEGER NOT NULL DEFAULT 0,
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE(run_id, node, attempt)
);

CREATE TABLE IF NOT EXISTS mutations (
    mutation_id TEXT    PRIMARY KEY,
    rule_id     TEXT    NOT NULL,
    verdict     TEXT    NOT NULL,
    arm_a       TEXT    NOT NULL,
    arm_b       TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    reverted    INTEGER NOT NULL,
    note        TEXT,
    action      TEXT
);
CREATE INDEX IF NOT EXISTS idx_mutations_rule ON mutations(rule_id);
"""

_COLUMNS = (
    "run_id",
    "unit_id",
    "project",
    "backend",
    "model",
    "tier",
    "kind",
    "ok",
    "exit_reason",
    "sec_total",
    "sec_provision",
    "cost_usd",
    "tokens_in",
    "tokens_out",
    "intervention",
    "created_at",
)

_MUT_COLUMNS = (
    "mutation_id",
    "rule_id",
    "verdict",
    "arm_a",
    "arm_b",
    "applied_at",
    "reverted",
    "note",
    "action",
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
    _migrate(conn)
    # Índice fora do SCHEMA: `action` nasce no _migrate, então em banco antigo o
    # CREATE INDEX no schema rodaria antes da coluna existir.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mutations_action ON mutations(action)")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Colunas que nasceram depois da tabela. Idempotente: roda a cada open.

    `CREATE TABLE IF NOT EXISTS` não altera tabela existente, então banco antigo
    fica sem as colunas novas — o `ALTER TABLE` guardado por `PRAGMA table_info`
    é o que faz banco velho e novo convergirem sem migração manual.
    """
    if not _has_column(conn, "mutations", "action"):
        conn.execute("ALTER TABLE mutations ADD COLUMN action TEXT")
        _backfill_action(conn)
        conn.commit()
    # `tokens_in`/`tokens_out` não têm backfill: o usage do run antigo não foi
    # gravado em lugar nenhum, então banco velho fica NULL — traço no report em
    # vez de zero inventado.
    for column in ("tokens_in", "tokens_out"):
        if not _has_column(conn, "runs", column):
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} INTEGER")
            conn.commit()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _backfill_action(conn: sqlite3.Connection) -> None:
    """Recupera a ação das linhas antigas, onde ela viajava dentro do `note`.

    Mesmo parse que o placar usava antes da coluna existir (`policy.action_of`),
    para o histórico não quebrar em duas eras. Note sem token → fica NULL.
    """
    from harness.improve import policy

    rows = conn.execute("SELECT mutation_id, note FROM mutations WHERE note IS NOT NULL").fetchall()
    updates = [
        (name, r["mutation_id"])
        for r in rows
        if (name := policy.action_of({"note": r["note"]})) is not None
    ]
    if updates:
        conn.executemany("UPDATE mutations SET action = ? WHERE mutation_id = ?", updates)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


def record_mutation(row: MutationRow, path: Path | None = None) -> bool:
    """Grava a mutação avaliada. False = `mutation_id` já estava lá.

    Não sobrescreve: o `mutation_id` é determinístico (regra + timestamp), então
    o resume do autopilot reexecutando o nó `record` não pode reescrever um
    veredito já emitido — a régua fala uma vez por experimento.
    """
    values = [_encode(getattr(row, c)) for c in _MUT_COLUMNS]
    placeholders = ", ".join("?" * len(_MUT_COLUMNS))
    with connect(path) as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO mutations ({', '.join(_MUT_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        return cur.rowcount == 1


def mutations(
    rule_id: str | None = None, limit: int | None = 500, path: Path | None = None
) -> list[MutationRow]:
    """Mutações mais recentes primeiro (ordem de gravação). `limit=None` = todas.

    Sem teto é para quem pergunta sobre o histórico INTEIRO, e não sobre a
    janela recente: "este `mutation_id` já foi julgado?" respondida por uma
    janela erra por omissão — o veredito antigo cai fora e a mutação parece
    pendente. Prior e listagem continuam com teto: lá a janela é o que se quer.
    """
    clause = " WHERE rule_id = ?" if rule_id is not None else ""
    params: list[object] = [rule_id] if rule_id is not None else []
    ceiling = ""
    if limit is not None:
        ceiling = " LIMIT ?"
        params.append(limit)
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM mutations{clause} ORDER BY rowid DESC{ceiling}", params
        ).fetchall()
    return [_mutation(r) for r in rows]


def record_node(
    run_id: str, node: str, payload: dict, path: Path | None = None, attempt: int = 0
) -> bool:
    """Marca `(run_id, node, attempt)` como executado. False = já estava lá."""
    with connect(path) as conn:
        return _insert_node(conn, run_id, node, payload, attempt)


def get_node(run_id: str, node: str, path: Path | None = None, attempt: int = 0) -> dict | None:
    """Payload gravado por `record_node`, ou None se o nó ainda não rodou."""
    with connect(path) as conn:
        row = _select_node(conn, run_id, node, attempt)
    return json.loads(row["payload"]) if row is not None else None


def node_payloads(node: str, path: Path | None = None) -> list[dict]:
    """Payloads de TODAS as passagens por um nó, na ordem de gravação.

    `get_node` responde por `(run_id, node, attempt)`; esta é a pergunta
    inversa — quem passou por este nó, em qualquer thread. É o que o guard de
    config sujo precisa: mutação aplicada e nunca julgada não tem linha em
    `mutations` (o crash foi antes do `record`), só o marcador do nó.
    """
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT payload FROM node_events WHERE node = ? ORDER BY rowid", (node,)
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def record_run_once(
    row: RunRow, node: str = "record", path: Path | None = None
) -> tuple[int, bool]:
    """Grava a linha do run e o marcador do nó na MESMA transação.

    Devolve `(row_id, gravou_agora)`. Duas transações separadas abririam uma
    janela em que um SIGKILL deixa a linha em `runs` sem marcador — o re-invoke
    do mesmo thread_id inseriria uma segunda linha para o mesmo run.
    """
    values = [_encode(getattr(row, c)) for c in _COLUMNS]
    placeholders = ", ".join("?" * len(_COLUMNS))
    with connect(path) as conn:
        # IMMEDIATE: o check-then-insert vira atômico também entre processos.
        conn.execute("BEGIN IMMEDIATE")
        saved = _select_node(conn, row.run_id, node, 0)
        if saved is not None:
            return int(json.loads(saved["payload"])["row_id"]), False
        cur = conn.execute(
            f"INSERT INTO runs ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        row_id = int(cur.lastrowid)
        _insert_node(conn, row.run_id, node, {"row_id": row_id}, 0)
        return row_id, True


def _insert_node(
    conn: sqlite3.Connection, run_id: str, node: str, payload: dict, attempt: int
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO node_events "
        "(run_id, node, attempt, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, node, attempt, json.dumps(payload, default=str), now_iso()),
    )
    return cur.rowcount == 1


def _select_node(
    conn: sqlite3.Connection, run_id: str, node: str, attempt: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT payload FROM node_events WHERE run_id = ? AND node = ? AND attempt = ?",
        (run_id, node, attempt),
    ).fetchone()


def _encode(value: object) -> object:
    return int(value) if isinstance(value, bool) else value


def _mutation(r: sqlite3.Row) -> MutationRow:
    return MutationRow(
        mutation_id=r["mutation_id"],
        rule_id=r["rule_id"],
        verdict=r["verdict"],
        arm_a=r["arm_a"],
        arm_b=r["arm_b"],
        applied_at=r["applied_at"],
        reverted=bool(r["reverted"]),
        note=r["note"],
        action=r["action"],
    )


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
        tokens_in=r["tokens_in"],
        tokens_out=r["tokens_out"],
        intervention=bool(r["intervention"]),
        created_at=r["created_at"],
    )
