"""Wrapper fino do driver da fila: o código vive em `harness/queue.py`.

O caminho de primeira classe agora é o CLI:

    uv run --extra deepagents harness queue --project oficina \
        --backend deepagents --model openai:qwen3.5-9b-mlx --deadline-s 3600

Este script continua porque há loop rodando com ele; a assinatura das env vars
é preservada. Env: PROJ (default oficina) · BACKEND (default deepagents) ·
MODEL (default openai:qwen3.5-9b-mlx, vazio = default do backend) · DEADLINE_S
(default 3600, teto do loop inteiro) · ATTEMPTS (default: teto de
config/graph.toml) · MOVE=0 (não mexe na fila: ensaio) · INTEGRATE=0 (não faz
merge da entrega aceita no branch default — a fila deixa de compor) ·
REGRESSION=0 (não re-roda os verifies de done/ depois da integração — conflito
semântico volta a passar silencioso).
"""

from __future__ import annotations

import os

from harness.queue import (
    DEFAULT_BACKEND,
    DEFAULT_DEADLINE_S,
    DEFAULT_MODEL,
    DEFAULT_PROJECT,
    run_queue,
)

PROJ = os.environ.get("PROJ", DEFAULT_PROJECT)
BACKEND = os.environ.get("BACKEND", DEFAULT_BACKEND)
MODEL = os.environ.get("MODEL", DEFAULT_MODEL)
DEADLINE_S = float(os.environ.get("DEADLINE_S", str(int(DEFAULT_DEADLINE_S))))
ATTEMPTS = os.environ.get("ATTEMPTS")
MOVE = os.environ.get("MOVE", "1") == "1"
INTEGRATE = os.environ.get("INTEGRATE", "1") == "1"
REGRESSION = os.environ.get("REGRESSION", "1") == "1"


def main() -> int:
    return run_queue(
        PROJ,
        backend=BACKEND,
        model=MODEL,
        deadline_s=DEADLINE_S,
        attempts=int(ATTEMPTS) if ATTEMPTS else None,
        move=MOVE,
        integrate_accepted=INTEGRATE,
        check_regression=REGRESSION,
    )


if __name__ == "__main__":
    raise SystemExit(main())
