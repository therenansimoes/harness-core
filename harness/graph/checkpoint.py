"""Checkpointer do grafo: SQLite em `<data_dir>/checkpoints.sqlite`.

O bootstrap de ambiente mora aqui e não só no cli porque o grafo pode ser usado
como biblioteca — telemetria de terceiro e desserialização permissiva não podem
depender de alguém ter passado pelo `main()`.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness.graph.state import Budget, Decision
from harness.types import ExecResult, Selection, UnitSpec, Verdict

CHECKPOINT_NAME = "checkpoints.sqlite"

# Allowlist do msgpack estrito: só estes tipos voltam do banco como objeto.
# Qualquer outra classe vira dict inerte — banco comprometido não executa código.
ALLOWED_TYPES = (UnitSpec, Selection, ExecResult, Verdict, Budget, Decision)


def bootstrap_env() -> None:
    """Desliga telemetria e liga msgpack estrito. Idempotente, respeita override."""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")


def checkpoint_path(data_dir: Path) -> Path:
    return Path(data_dir) / CHECKPOINT_NAME


@contextmanager
def open_checkpointer(data_dir: Path) -> Iterator[object]:
    """Abre o SqliteSaver do `data_dir`. Fecha a conexão na saída do bloco."""
    bootstrap_env()
    # Import tardio: as vars de ambiente acima são lidas no import do langgraph.
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = checkpoint_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        yield SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_TYPES))
    finally:
        conn.close()
