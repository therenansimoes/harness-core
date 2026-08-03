"""Caixa de entrada universal: `data/inbox/*.json` acorda o harness.

Qualquer coisa que saiba escrever um arquivo acorda o loop — git hook,
webhook, `curl` via `serve_webhook`, humano, MCP — largando um JSON aqui.
Tipos documentados:

    {"type": "run_failed", "unit_id": "u1"}    # unidade falhou (CI, hook)
    {"type": "research", "topic": "timeouts"}  # pesquisar e destilar skill
    {"type": "improve"}                        # um ciclo de auto-melhoria

Processado vai para `done/`; torto (JSON inválido, sem `type`, tipo sem
handler, handler que explode) vai para `bad/` — o processador nunca crasha
por causa de um evento. Handlers são INJETADOS: o dispatcher não sabe o que
os eventos significam, só quem assina sabe (`default_handlers` é a fábrica).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

Handler = Callable[[dict], None]


def process_inbox(inbox_dir: Path, handlers: dict[str, Handler]) -> int:
    """Despacha cada `*.json` (ordem de mtime) pelo campo `type`.

    Devolve quantos eventos foram processados com sucesso.
    """
    inbox_dir = Path(inbox_dir)
    done = inbox_dir / "done"
    bad = inbox_dir / "bad"
    ok = 0
    files = sorted(
        (p for p in inbox_dir.glob("*.json") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            handler = handlers[payload["type"]]
        except Exception:
            _move(path, bad)
            continue
        try:
            handler(payload)
        except Exception:
            _move(path, bad)
            continue
        _move(path, done)
        ok += 1
    return ok


def _move(path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / path.name
    i = 1
    while target.exists():
        target = dest_dir / f"{path.stem}.{i}{path.suffix}"
        i += 1
    shutil.move(str(path), str(target))


# --------------------------------------------------------------------------
# Handlers de fábrica


def default_handlers() -> dict[str, Handler]:
    """Mapa padrão de tipo → handler. Injetável: teste passa os seus."""
    return {
        "improve": _handle_improve,
        "research": _handle_research,
        "run_failed": _handle_run_failed,
    }


def _handle_improve(payload: dict) -> None:
    """Um ciclo de melhoria em subprocess: isola o watcher do loop."""
    subprocess.run(
        ["uv", "run", "harness", "improve", "--cycles", "1", "--backend", "mock"],
        check=True,
        timeout=1800,
    )


def _handle_research(payload: dict) -> None:
    """Ação `research` via registry. Sem tópico ou sem gradiente → no-op."""
    from harness.improve.target import get_action

    act = get_action("research")
    topic = str(payload.get("topic") or "").strip() or None
    proposal = act.propose(topic=topic)
    if proposal is None:
        return
    act.apply(proposal, backend="mock")


def _handle_run_failed(payload: dict) -> None:
    """Só registra: virar gradiente é papel do ledger, não do evento."""
    print(f"[inbox] run_failed unit_id={payload.get('unit_id', '?')}", file=sys.stderr)


# --------------------------------------------------------------------------
# Webhook mínimo: POST → arquivo no inbox


def serve_webhook(
    port: int,
    inbox_dir: Path,
    max_requests: int | None = None,
    on_bind: Callable[[int], None] | None = None,
) -> None:
    """HTTP stdlib: corpo do POST vira `web-<ns>.json` no inbox, responde 202.

    Não processa nada — só deposita; quem processa é o watcher do inbox.
    `port=0` + `on_bind` = porta efêmera p/ teste; `max_requests` p/ não
    servir para sempre.
    """
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - contrato do BaseHTTPRequestHandler
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            try:
                json.loads(body.decode("utf-8"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return
            (inbox / f"web-{time.time_ns()}.json").write_bytes(body)
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass  # silêncio: stderr é do watcher

    server = HTTPServer(("127.0.0.1", port), _Handler)
    try:
        if on_bind is not None:
            on_bind(server.server_address[1])
        if max_requests is None:
            server.serve_forever()
        else:
            for _ in range(max_requests):
                server.handle_request()
    finally:
        server.server_close()
