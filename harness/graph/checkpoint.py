"""Checkpointer do grafo: fábrica com SQLite por default e "memory" via config.

SQLite persiste em `<data_dir>/checkpoints.sqlite` e retoma entre processos;
"memory" (config/graph.toml, chave top-level `checkpointer`) é efêmero para
testes/execução descartável — NÃO retoma após crash/kill entre processos
(resume-after-crash exige sqlite).

O bootstrap de ambiente mora aqui e não só no cli porque o grafo pode ser usado
como biblioteca — telemetria de terceiro e desserialização permissiva não podem
depender de alguém ter passado pelo `main()`.
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness.graph.state import Budget, Decision
from harness.paths import config_dir
from harness.types import ExecResult, Selection, UnitSpec, Verdict

CHECKPOINT_NAME = "checkpoints.sqlite"

# Duplicado de run_graph.py de propósito — importar de lá criaria ciclo
# (run_graph importa checkpoint; checkpoint nunca importa run_graph).
GRAPH_TOML = "graph.toml"

CHECKPOINTER_SQLITE = "sqlite"
CHECKPOINTER_MEMORY = "memory"
CHECKPOINTER_KINDS = (CHECKPOINTER_SQLITE, CHECKPOINTER_MEMORY)
DEFAULT_CHECKPOINTER = CHECKPOINTER_SQLITE

# Allowlist do msgpack estrito: só estes tipos voltam do banco como objeto.
# Qualquer outra classe vira dict inerte — banco comprometido não executa código.
ALLOWED_TYPES = (UnitSpec, Selection, ExecResult, Verdict, Budget, Decision)


class CheckpointerConfigError(ValueError):
    """`checkpointer` inválido em config/graph.toml. Fail-closed de propósito:
    config torta aqui não pode degradar em silêncio para outro backend de
    persistência — o resume depende dele."""


def bootstrap_env() -> None:
    """Desliga telemetria e liga msgpack estrito. Idempotente, respeita override."""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")


def checkpoint_path(data_dir: Path) -> Path:
    return Path(data_dir) / CHECKPOINT_NAME


def load_checkpointer_kind(path: Path | None = None) -> str:
    """Resolve o backend do checkpointer a partir de `config/graph.toml`.

    Arquivo ausente/ilegível/TOML torto ou chave ausente => DEFAULT_CHECKPOINTER
    (comportamento de fábrica, igual ao load_policy). Chave PRESENTE com valor
    fora de CHECKPOINTER_KINDS => CheckpointerConfigError — fail-closed no parse,
    antes de qualquer invoke do grafo.
    """
    p = Path(path) if path is not None else config_dir() / GRAPH_TOML
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return DEFAULT_CHECKPOINTER
    raw = data.get("checkpointer")
    if raw is None:
        return DEFAULT_CHECKPOINTER
    if raw not in CHECKPOINTER_KINDS:
        raise CheckpointerConfigError(
            f"checkpointer inválido em {p}: {raw!r} (válidos: {', '.join(CHECKPOINTER_KINDS)})"
        )
    return raw


@contextmanager
def open_checkpointer(data_dir: Path, kind: str | None = None) -> Iterator[object]:
    """Abre o checkpointer do `data_dir` (sqlite fecha a conexão na saída).

    `kind=None` lê `config/graph.toml`; valor inválido levanta
    CheckpointerConfigError já na entrada do `with` — antes de compilar/invocar
    o grafo, antes de abrir qualquer arquivo.
    """
    bootstrap_env()
    if kind is None:
        kind = load_checkpointer_kind()
    elif kind not in CHECKPOINTER_KINDS:
        raise CheckpointerConfigError(
            f"checkpointer inválido: {kind!r} (válidos: {', '.join(CHECKPOINTER_KINDS)})"
        )
    # Import tardio: as vars de ambiente acima são lidas no import do langgraph.
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    # Allowlist aplicada aos DOIS backends — store comprometido não executa código.
    serde = JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_TYPES)

    if kind == CHECKPOINTER_MEMORY:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver(serde=serde)
        return  # nenhum arquivo criado, nada a fechar

    from langgraph.checkpoint.sqlite import SqliteSaver

    path = checkpoint_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        yield SqliteSaver(conn, serde=serde)
    finally:
        conn.close()
