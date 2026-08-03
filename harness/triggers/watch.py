"""Vigias por polling: ledger (falhas recentes) e inbox (eventos).

`max_iters` e `sleep_fn` injetáveis existem para o teste não dormir — o
mesmo motivo do mock backend no grafo: o relógio nunca é parte do contrato.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable

from harness.triggers.inbox import Handler, process_inbox


def _recent_failures(db_path: Path, window: int) -> tuple[int, int]:
    """(falhas na janela recente, maior rowid visto). Janela por rowid desc."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, ok FROM runs ORDER BY id DESC LIMIT ?", (window,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0, 0
    return sum(1 for _rid, ok in rows if not ok), int(rows[0][0])


def watch_ledger(
    db_path: Path,
    on_failures: Callable[[dict], None],
    threshold: int = 3,
    window: int = 50,
    poll_s: float = 30,
    max_iters: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Poll no runs.sqlite; falhas na janela >= threshold → `on_failures(stats)`.

    Dedupe por marca d'água de rowid: depois de disparar só dispara de novo
    quando existir linha NOVA no ledger e a condição continuar valendo — a
    mesma janela não acorda ninguém duas vezes.
    """
    db = Path(db_path)
    watermark = -1
    i = 0
    while max_iters is None or i < max_iters:
        i += 1
        if db.exists():
            try:
                fails, max_id = _recent_failures(db, window)
            except sqlite3.Error:
                fails, max_id = 0, watermark  # db no meio de um write: pula
            if fails >= threshold and max_id > watermark:
                watermark = max_id
                on_failures(
                    {
                        "fails": fails,
                        "window": window,
                        "threshold": threshold,
                        "max_rowid": max_id,
                    }
                )
        if max_iters is None or i < max_iters:
            sleep_fn(poll_s)


def watch_inbox(
    inbox_dir: Path,
    handlers: dict[str, Handler],
    poll_s: float = 30,
    max_iters: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Poll no inbox reusando `process_inbox`: o dispatcher já não crasha."""
    inbox = Path(inbox_dir)
    i = 0
    while max_iters is None or i < max_iters:
        i += 1
        process_inbox(inbox, handlers)
        if max_iters is None or i < max_iters:
            sleep_fn(poll_s)
