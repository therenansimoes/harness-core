"""Vigias por polling: ledger (falhas recentes) e inbox (eventos).

`max_iters` e `sleep_fn` injetáveis existem para o teste não dormir — o
mesmo motivo do mock backend no grafo: o relógio nunca é parte do contrato.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
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


def should_dream(
    db: Path,
    now: datetime | None = None,
    min_hours: int = 24,
    min_runs: int = 5,
    dreams_dir: Path | None = None,
) -> bool:
    """Está na hora de consolidar a memória episódica?

    Duas condições, as duas obrigatórias: passaram `min_hours` desde o último
    relatório de sono E existem `min_runs` runs no ledger desde ele. Tempo sem
    run é sono que consolidaria o mesmo material de novo; run sem tempo é sono
    disparando em rajada no meio de uma bateria.

    O último sono é descoberto por `<data_dir>/dreams/*.md` (o data dir é o
    diretório do próprio `db`, que é o namespace da episódica): o relatório É o
    registro, então não há segunda fonte de verdade a manter em sincronia.
    Nunca sonhou = a condição de tempo já está satisfeita, só a de runs conta.

    Erro de leitura conta como 0 runs — fail-closed. Não sonhar é no-op; sonhar
    com o ledger ilegível é arquivar memória sem evidência.
    """
    path = Path(db)
    at = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)

    last = _last_dream_at(dreams_dir if dreams_dir is not None else path.parent / "dreams")
    if last is not None and at - last < timedelta(hours=min_hours):
        return False
    return _runs_since(path, last) >= min_runs


def _last_dream_at(dreams: Path) -> datetime | None:
    """mtime do relatório mais recente, ou None se nunca sonhou.

    mtime e não o nome do arquivo: o nome é gerado pelo relógio injetado do
    `dream`, e um teste (ou um replay) que sonha com data do passado não pode
    fazer o gatilho achar que o último sono foi em 1970.
    """
    reports = sorted(Path(dreams).glob("*.md"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return None
    return datetime.fromtimestamp(reports[-1].stat().st_mtime, tz=timezone.utc)


def _runs_since(db_path: Path, since: datetime | None) -> int:
    """Runs no ledger depois de `since` (todas, se None). Erro/ausência = 0.

    Compara `created_at` como texto contra o ISO em UTC: é o mesmo formato que
    o `store.now_iso` grava, e converter linha por linha custaria o ledger
    inteiro para uma decisão de gatilho.
    """
    if not db_path.exists():
        return 0
    sql = "SELECT count(*) FROM runs"
    params: tuple = ()
    if since is not None:
        sql += " WHERE created_at > ?"
        params = (since.astimezone(timezone.utc).isoformat(timespec="seconds"),)
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


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
