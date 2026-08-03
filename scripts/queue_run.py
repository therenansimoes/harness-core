"""Consome a fila de um projeto pelo GRAFO (`run_unit`), uma unidade por vez.

Por que existe: `harness run` é inline (`run_once`) e não entra em worktree, e
`harness improve`/`evolve` leem `benchmarks/held_in` — nenhum dos dois consome
`projects/<nome>/queue`. Este driver é o caminho que ativa o modo projeto:
worktree do repo real em branch efêmera, e no accept a entrega vira
`harness/<unit_id>` para review humano.

Fila progressiva: unidade que não aceita PARA o loop (a próxima depende dela).
Aceita vai para `queue/done/`, travada vai para `queue/stuck/` — os buckets que
`harness status` conta.

    PROJ=oficina BACKEND=deepagents MODEL=ollama:qwen2.5:3b DEADLINE_S=3600 \
        uv run --extra deepagents python scripts/queue_run.py

Env: PROJ (default oficina) · BACKEND (default deepagents) · MODEL
(default ollama:qwen2.5:3b, vazio = default do backend) · DEADLINE_S (default
3600, teto do loop inteiro) · ATTEMPTS (default: teto de config/graph.toml) ·
MOVE=0 (não mexe na fila: ensaio).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.projects import QUEUE_DONE, QUEUE_STUCK, UNIT_FILE, get_project

PROJ = os.environ.get("PROJ", "oficina")
BACKEND = os.environ.get("BACKEND", "deepagents")
MODEL = os.environ.get("MODEL", "ollama:qwen2.5:3b")
DEADLINE_S = float(os.environ.get("DEADLINE_S", "3600"))
ATTEMPTS = os.environ.get("ATTEMPTS")
MOVE = os.environ.get("MOVE", "1") == "1"


def _pendentes(queue: Path) -> list[Path]:
    return sorted(
        p for p in queue.iterdir()
        if p.is_dir() and p.name not in (QUEUE_DONE, QUEUE_STUCK)
        and (p / UNIT_FILE).is_file()
    )


def main() -> int:
    proj = get_project(PROJ)
    queue = proj.queue_dir
    if not queue or not queue.is_dir():
        print(f"fila de {PROJ} não existe: {queue}")
        return 1
    data = store.data_dir()
    t0 = time.time()
    for unit_dir in _pendentes(queue):
        gasto = time.time() - t0
        if gasto > DEADLINE_S:
            print(f"deadline {DEADLINE_S:.0f}s estourado em {gasto:.0f}s — para aqui")
            break
        # thread_id com timestamp do loop: retomada é opt-in (mesmo id), e sem
        # isso um segundo loop no mesmo dia reaproveitaria checkpoint antigo.
        thread = f"{PROJ}-{unit_dir.name}-{int(t0)}"
        state = run_unit(
            unit_dir, BACKEND, MODEL or None, data, thread_id=thread,
            max_attempts=int(ATTEMPTS) if ATTEMPTS else None,
        )
        decision = state["decision"]
        print(f"{unit_dir.name}: {decision.action} — {decision.reason} thread={thread}")
        if not MOVE:
            continue
        bucket = queue / (QUEUE_DONE if decision.action == "accept" else QUEUE_STUCK)
        bucket.mkdir(exist_ok=True)
        shutil.move(str(unit_dir), str(bucket / unit_dir.name))
        if decision.action != "accept":
            print(f"{unit_dir.name} travou — fila progressiva, o resto depende dela")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
