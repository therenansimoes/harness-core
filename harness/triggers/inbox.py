"""Caixa de entrada universal: `data/inbox/*.json` acorda o harness.

Qualquer coisa que saiba escrever um arquivo acorda o loop — git hook,
webhook (`triggers.webhook.serve_webhook`), humano, MCP — largando um JSON aqui.
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
from collections.abc import Callable
from pathlib import Path

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
