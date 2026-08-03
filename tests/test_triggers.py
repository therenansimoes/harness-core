"""Gatilhos: inbox despacha e move, ledger dispara com dedupe, webhook grava.

O webhook é a única porta que desconfia de quem chama: token em tempo
constante, rate-limit e teto de corpo. `screen_request` é puro — o teste
julga status sem abrir socket; só o caminho felizão sobe servidor de verdade.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from harness.triggers import (
    TOKEN_HEADER,
    WEBHOOK_TOKEN_ENV,
    RateLimiter,
    WebhookConfig,
    default_handlers,
    load_webhook_config,
    process_inbox,
    screen_request,
    serve_webhook,
    token_ok,
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


def _cfg(**kw: object) -> WebhookConfig:
    base = {"token": "s3gr3d0", "rate_limit": 3, "rate_window_s": 60.0}
    return WebhookConfig(**{**base, **kw})  # type: ignore[arg-type]


def _limiter(cfg: WebhookConfig, clock: object = None) -> RateLimiter:
    return RateLimiter(
        cfg.rate_limit,
        cfg.rate_window_s,
        clock=clock if clock is not None else (lambda: 0.0),  # type: ignore[arg-type]
    )


def test_screen_request_token_certo_passa_errado_e_ausente_recusam() -> None:
    cfg = _cfg(rate_limit=99)
    lim = _limiter(cfg)
    assert screen_request(cfg, lim, "s3gr3d0", 10, "1.2.3.4") == 202
    assert screen_request(cfg, lim, "s3gr3d1", 10, "1.2.3.4") == 403
    assert screen_request(cfg, lim, "", 10, "1.2.3.4") == 403
    assert screen_request(cfg, lim, None, 10, "1.2.3.4") == 403
    # Prefixo certo não vale: compare_digest não é `startswith`.
    assert screen_request(cfg, lim, "s3gr3d", 10, "1.2.3.4") == 403


def test_sem_token_configurado_recusa_tudo_fail_closed(tmp_path: Path) -> None:
    fechado = load_webhook_config(path=tmp_path / "nao-existe.toml", env={})
    assert fechado.token == ""
    lim = _limiter(fechado)
    assert screen_request(fechado, lim, "qualquer-coisa", 10) == 403
    assert screen_request(fechado, lim, None, 10) == 403
    assert screen_request(fechado, lim, "", 10) == 403
    assert not token_ok(fechado, "")  # token vazio não casa com token vazio


def test_load_webhook_config_toml_e_override_por_env(tmp_path: Path) -> None:
    p = tmp_path / "triggers.toml"
    p.write_text(
        "[webhook]\ntoken = 'do-toml'\nrate_limit = 7\nrate_window_s = 5\n"
        "max_body_bytes = 100\n",
        encoding="utf-8",
    )
    cfg = load_webhook_config(path=p, env={})
    assert (cfg.token, cfg.rate_limit, cfg.rate_window_s, cfg.max_body_bytes) == (
        "do-toml",
        7,
        5.0,
        100,
    )
    env = load_webhook_config(path=p, env={WEBHOOK_TOKEN_ENV: " do-env "})
    assert env.token == "do-env"  # env manda, e sem espaço em volta
    # Número torto cai no default; token continua sem default.
    p.write_text("[webhook]\nrate_limit = 'muitos'\n", encoding="utf-8")
    torto = load_webhook_config(path=p, env={})
    assert (torto.rate_limit, torto.token) == (WebhookConfig().rate_limit, "")


def test_rate_limit_estoura_com_429_e_reabre_na_janela_seguinte() -> None:
    agora = [0.0]
    cfg = _cfg(rate_limit=3, rate_window_s=60.0)
    lim = _limiter(cfg, clock=lambda: agora[0])
    got = [screen_request(cfg, lim, "s3gr3d0", 10, "9.9.9.9") for _ in range(4)]
    assert got == [202, 202, 202, 429]
    # Outro IP tem freio próprio.
    assert screen_request(cfg, lim, "s3gr3d0", 10, "8.8.8.8") == 202
    agora[0] = 60.1  # janela virou
    assert screen_request(cfg, lim, "s3gr3d0", 10, "9.9.9.9") == 202
    # Freio vem antes do token: adivinhação também toma 429.
    for _ in range(2):
        lim.allow("7.7.7.7")
    lim.allow("7.7.7.7")
    assert screen_request(cfg, lim, "errado", 10, "7.7.7.7") == 429


def test_corpo_maior_que_o_teto_e_413_antes_do_token() -> None:
    cfg = _cfg(max_body_bytes=64 * 1024)
    lim = _limiter(cfg)
    assert screen_request(cfg, lim, "s3gr3d0", 64 * 1024, "1.1.1.1") == 202
    assert screen_request(cfg, lim, "s3gr3d0", 64 * 1024 + 1, "1.1.1.1") == 413
    # Sem token válido o tamanho já derruba: 413 vem antes do 403.
    assert screen_request(cfg, lim, "errado", 64 * 1024 + 1, "1.1.1.1") == 413


def _post(port: int, body: bytes, token: str | None) -> int:
    headers = {} if token is None else {TOKEN_HEADER: token}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _serve(
    tmp_path: Path, cfg: WebhookConfig, max_requests: int
) -> tuple[int, threading.Thread]:
    bound: list[int] = []
    ready = threading.Event()

    def on_bind(port: int) -> None:
        bound.append(port)
        ready.set()

    t = threading.Thread(
        target=serve_webhook,
        args=(0, tmp_path),
        kwargs={"max_requests": max_requests, "on_bind": on_bind, "config": cfg},
        daemon=True,
    )
    t.start()
    assert ready.wait(5)
    return bound[0], t


def test_serve_webhook_grava_post_autenticado_e_recusa_sem_token(
    tmp_path: Path,
) -> None:
    port, t = _serve(tmp_path, _cfg(rate_limit=99), max_requests=2)
    body = json.dumps({"type": "improve"}).encode("utf-8")
    assert _post(port, body, None) == 403
    assert _post(port, body, "s3gr3d0") == 202
    t.join(timeout=5)
    assert not t.is_alive()
    files = list(tmp_path.glob("web-*.json"))
    assert len(files) == 1  # a recusada não deixou arquivo no inbox
    assert json.loads(files[0].read_text(encoding="utf-8")) == {"type": "improve"}
