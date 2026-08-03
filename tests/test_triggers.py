"""Gatilhos: inbox despacha e move, ledger dispara com dedupe, webhook grava."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from harness.triggers import (
    default_handlers,
    process_inbox,
    serve_webhook,
    watch_inbox,
    watch_ledger,
)


# ---------------------------------------------------------------- inbox


def _drop(inbox: Path, name: str, payload: dict) -> Path:
    p = inbox / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_process_inbox_despacha_e_move_para_done(tmp_path: Path) -> None:
    seen: list[dict] = []
    _drop(tmp_path, "a.json", {"type": "research", "topic": "x"})
    _drop(tmp_path, "b.json", {"type": "improve"})
    n = process_inbox(tmp_path, {"research": seen.append, "improve": seen.append})
    assert n == 2
    assert {p["type"] for p in seen} == {"research", "improve"}
    assert sorted(p.name for p in (tmp_path / "done").glob("*.json")) == [
        "a.json",
        "b.json",
    ]
    assert list(tmp_path.glob("*.json")) == []


def test_process_inbox_torto_vai_para_bad_sem_crashar(tmp_path: Path) -> None:
    (tmp_path / "lixo.json").write_text("{nao é json", encoding="utf-8")
    _drop(tmp_path, "semtipo.json", {"unit_id": "u1"})
    _drop(tmp_path, "desconhecido.json", {"type": "marte"})
    _drop(tmp_path, "ok.json", {"type": "improve"})
    n = process_inbox(tmp_path, {"improve": lambda p: None})
    assert n == 1
    assert sorted(p.name for p in (tmp_path / "bad").glob("*.json")) == [
        "desconhecido.json",
        "lixo.json",
        "semtipo.json",
    ]
    assert [p.name for p in (tmp_path / "done").glob("*.json")] == ["ok.json"]


def test_handler_que_explode_nao_derruba_o_processador(tmp_path: Path) -> None:
    def bomba(payload: dict) -> None:
        raise RuntimeError("boom")

    ok_calls: list[dict] = []
    _drop(tmp_path, "a.json", {"type": "bomba"})
    _drop(tmp_path, "b.json", {"type": "improve"})
    n = process_inbox(tmp_path, {"bomba": bomba, "improve": ok_calls.append})
    assert n == 1
    assert len(ok_calls) == 1
    assert [p.name for p in (tmp_path / "bad").glob("*.json")] == ["a.json"]


def test_default_handlers_cobre_tipos_documentados() -> None:
    h = default_handlers()
    assert set(h) == {"improve", "research", "run_failed"}
    h["run_failed"]({"type": "run_failed", "unit_id": "u1"})  # não explode


def test_watch_inbox_reusa_process_inbox_sem_dormir(tmp_path: Path) -> None:
    seen: list[dict] = []
    sleeps: list[float] = []
    _drop(tmp_path, "a.json", {"type": "improve"})
    watch_inbox(
        tmp_path,
        {"improve": seen.append},
        poll_s=99,
        max_iters=2,
        sleep_fn=sleeps.append,
    )
    assert len(seen) == 1
    assert sleeps == [99]


# ---------------------------------------------------------------- ledger


def _mk_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ok INTEGER)")
    return conn


def _add_runs(conn: sqlite3.Connection, oks: list[int]) -> None:
    conn.executemany("INSERT INTO runs (ok) VALUES (?)", [(o,) for o in oks])
    conn.commit()


def test_watch_ledger_dispara_no_threshold_e_deduplica(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    conn = _mk_db(db)
    _add_runs(conn, [1, 0, 0, 0, 1])  # 3 falhas
    fired: list[dict] = []
    watch_ledger(
        db,
        fired.append,
        threshold=3,
        window=50,
        poll_s=0,
        max_iters=3,
        sleep_fn=lambda s: None,
    )
    conn.close()
    assert len(fired) == 1  # 3 iterações, mesma janela: UMA chamada
    assert fired[0]["fails"] == 3
    assert fired[0]["threshold"] == 3


def test_watch_ledger_redispara_com_linha_nova(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    conn = _mk_db(db)
    _add_runs(conn, [0, 0, 0])
    fired: list[dict] = []

    def sleep_fn(_s: float) -> None:
        if len(fired) == 1:
            _add_runs(conn, [0])  # linha nova move a marca d'água

    watch_ledger(
        db,
        fired.append,
        threshold=3,
        window=50,
        poll_s=0,
        max_iters=4,
        sleep_fn=sleep_fn,
    )
    conn.close()
    assert len(fired) == 2
    assert fired[1]["max_rowid"] > fired[0]["max_rowid"]


def test_watch_ledger_abaixo_do_threshold_nao_dispara(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    conn = _mk_db(db)
    _add_runs(conn, [1, 1, 0])
    fired: list[dict] = []
    watch_ledger(
        db, fired.append, threshold=3, max_iters=2, sleep_fn=lambda s: None
    )
    conn.close()
    assert fired == []


def test_watch_ledger_sem_db_nao_crasha(tmp_path: Path) -> None:
    watch_ledger(
        tmp_path / "nao-existe.sqlite",
        lambda s: pytest.fail("não devia disparar"),
        max_iters=2,
        sleep_fn=lambda s: None,
    )


# ---------------------------------------------------------------- webhook


def test_serve_webhook_grava_post_no_inbox(tmp_path: Path) -> None:
    bound: list[int] = []
    ready = threading.Event()

    def on_bind(port: int) -> None:
        bound.append(port)
        ready.set()

    t = threading.Thread(
        target=serve_webhook,
        args=(0, tmp_path),
        kwargs={"max_requests": 1, "on_bind": on_bind},
        daemon=True,
    )
    t.start()
    assert ready.wait(5)
    body = json.dumps({"type": "improve"}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{bound[0]}/", data=body, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 202
    t.join(timeout=5)
    assert not t.is_alive()
    files = list(tmp_path.glob("web-*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == {"type": "improve"}
