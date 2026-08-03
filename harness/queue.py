"""Driver da fila de um projeto: consome `projects/<nome>/queue` pelo GRAFO.

Por que existe: `harness run` é inline (`run_once`) e não entra em worktree, e
`harness improve`/`evolve` leem `benchmarks/held_in` — nenhum dos dois consome
a fila do projeto. Este driver é o caminho que ativa o modo projeto: worktree do
repo real em branch efêmera, e no accept a entrega vira `harness/<unit_id>` para
review humano.

Fila progressiva: unidade que não aceita PARA o loop (a próxima depende dela).
Depois do accept a entrega é integrada no branch default do repo (`integrate`),
senão a unidade seguinte sairia de um HEAD sem o trabalho da anterior.
Aceita vai para `queue/done/`, travada vai para `queue/stuck/` — os buckets que
`harness status` conta.

Módulo separado de `projects.py` de propósito: aquele é o registro (+ entrega em
branch) e não conhece o grafo; aqui o driver importa `run_graph`, que já importa
`projects` — juntar os dois fecharia ciclo.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.projects import (
    QUEUE_DONE,
    QUEUE_STUCK,
    UNIT_FILE,
    IntegrateError,
    get_project,
    integrate,
)

DEFAULT_PROJECT = "oficina"
DEFAULT_BACKEND = "deepagents"
DEFAULT_MODEL = "ollama:qwen2.5:3b"
DEFAULT_DEADLINE_S = 3600.0


def pending(queue: Path) -> list[Path]:
    """Unidades ainda na fila, em ordem de nome (a ordem é a dependência)."""
    return sorted(
        p for p in queue.iterdir()
        if p.is_dir() and p.name not in (QUEUE_DONE, QUEUE_STUCK)
        and (p / UNIT_FILE).is_file()
    )


def run_queue(
    project: str = DEFAULT_PROJECT,
    backend: str = DEFAULT_BACKEND,
    model: str | None = DEFAULT_MODEL,
    deadline_s: float = DEFAULT_DEADLINE_S,
    attempts: int | None = None,
    move: bool = True,
    integrate_accepted: bool = True,
    projects_path: Path | None = None,
) -> int:
    """Roda a fila do projeto até acabar, travar ou estourar o deadline.

    `move=False` é ensaio: roda igual e não mexe na fila. Devolve o exit code.

    `integrate_accepted=False` desliga o merge da entrega no branch default: a
    fila volta a ser uma sequência de branches que não compõem (só faz sentido
    quando o objetivo é justamente inspecionar cada entrega isolada).
    """
    proj = get_project(project, projects_path)
    queue = proj.queue_dir
    if not queue or not queue.is_dir():
        print(f"fila de {project} não existe: {queue}")
        return 1
    data = store.data_dir()
    t0 = time.time()
    for unit_dir in pending(queue):
        gasto = time.time() - t0
        if gasto > deadline_s:
            print(f"deadline {deadline_s:.0f}s estourado em {gasto:.0f}s — para aqui")
            break
        # thread_id com timestamp do loop: retomada é opt-in (mesmo id), e sem
        # isso um segundo loop no mesmo dia reaproveitaria checkpoint antigo.
        thread = f"{project}-{unit_dir.name}-{int(t0)}"
        state = run_unit(
            unit_dir, backend, model or None, data, thread_id=thread,
            max_attempts=attempts,
        )
        decision = state["decision"]
        print(f"{unit_dir.name}: {decision.action} — {decision.reason} thread={thread}")
        if not move:
            continue
        # A entrega precisa entrar no branch default antes da próxima unidade: o
        # worktree dela sai do HEAD do repo, então sem merge o trabalho aceito
        # não compõe. Integração que falha derruba a unidade em stuck/ e para a
        # fila — o mesmo tratamento de uma unidade que travou.
        ok = True
        if decision.action == "accept" and integrate_accepted:
            unit_id = getattr(state.get("unit"), "id", None) or unit_dir.name
            try:
                print(integrate(proj, unit_id))
            except (IntegrateError, ValueError) as exc:
                ok = False
                print(f"{unit_dir.name}: integração falhou — {exc}")
        aceita = decision.action == "accept" and ok
        bucket = queue / (QUEUE_DONE if aceita else QUEUE_STUCK)
        bucket.mkdir(exist_ok=True)
        shutil.move(str(unit_dir), str(bucket / unit_dir.name))
        if not aceita:
            print(f"{unit_dir.name} travou — fila progressiva, o resto depende dela")
            break
    return 0
