"""A/B de execução rodado pelo próprio harness: dois braços, a MESMA unidade.

O cálculo do veredito é da régua (`ruler/wilson.py`); o que este módulo faz é
gerar a amostra de um jeito que a régua possa acreditar. Duas escolhas mandam:

1. **Ordem alternada A,B,A,B…** em vez de n vezes A e depois n vezes B. Ambiente
   que degrada no meio do experimento (cache frio, máquina ocupada, rate limit
   do provedor) pune os dois braços igual em vez de virar vantagem de braço.
2. **Toda run entra no ledger** com `backend`/`model` preenchidos. O A/B paga
   duas vezes: dá o veredito agora e vira prior do router depois.

Sucesso do braço = `RunRow.ok`, que é a decisão do gate — não o "terminei" do
executor.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from harness.ledger import store
from harness.ruler.wilson import MIN_N, AbVerdict, Arm, decide_ab
from harness.types import RunRow, Selection

ARMS = ("a", "b")


@dataclass(frozen=True)
class ArmSpec:
    """Um braço do experimento: quem executa. `tier` não muda a execução — vai
    para a linha do ledger porque a chave do prior é `(kind, tier, backend)`.
    """

    backend: str
    model: str | None = None
    tier: str | None = None
    max_turns: int | None = None


@dataclass(frozen=True)
class AbReport:
    """Resultado do experimento. `arm_a`/`arm_b` são os braços da régua
    (sucessos em tentativas); `rows_*` são as linhas gravadas, em ordem."""

    verdict: AbVerdict
    arm_a: Arm
    arm_b: Arm
    rows_a: tuple[RunRow, ...]
    rows_b: tuple[RunRow, ...]
    sec_total: float


def run_ab(
    unit_dir: Path | str,
    arm_a: ArmSpec | Selection,
    arm_b: ArmSpec | Selection,
    n: int,
    data_dir: Path | str | None = None,
    *,
    min_n: int = MIN_N,
    project: str | None = None,
    on_run: Callable[[str, int, RunRow], None] | None = None,
    before_run: Callable[[str, int], None] | None = None,
    intervention: bool = False,
) -> AbReport:
    """Roda `n` vezes cada braço, alternando, e devolve o veredito de Wilson.

    `data_dir` None = o ledger default (`$HARNESS_DATA_DIR`). `on_run` recebe
    `(braço, i, row)` a cada run — é como a CLI imprime progresso sem que este
    módulo saiba o que é stdout.

    `before_run` recebe `(braço, i)` ANTES de cada run e é o que permite testar
    dimensão que não cabe no `ArmSpec`: o autopilot usa para ligar/desligar a
    mutação de config entre uma run e outra, preservando a alternância. Ele
    roda dentro do experimento, então exceção dele aborta o A/B inteiro — é
    intencional, braço montado errado não vira amostra.

    `intervention=True` marca as linhas do ledger como "teve humano no loop".
    Quem sabe disso é o chamador (o grafo que foi retomado por um resume), não
    a run.
    """
    # Import tardio: o cli é quem chama o A/B, então o A/B não importa o cli no
    # topo (ciclo) — mesmo padrão do graph/run_graph.py.
    from harness.cli import DEFAULT_MAX_TURNS, load_unit, run_once

    if n <= 0:
        raise ValueError(f"n tem que ser positivo: {n}")

    unit = load_unit(Path(unit_dir))
    specs = tuple(_spec(arm) for arm in (arm_a, arm_b))
    db = Path(data_dir) / store.DB_NAME if data_dir is not None else None
    rows: dict[str, list[RunRow]] = {label: [] for label in ARMS}

    t0 = time.monotonic()
    for i in range(1, n + 1):
        for label, spec in zip(ARMS, specs):
            if before_run is not None:
                before_run(label, i)
            outcome = run_once(
                unit,
                spec.backend,
                spec.model,
                project=project,
                tier=spec.tier,
                max_turns=spec.max_turns or DEFAULT_MAX_TURNS,
            )
            row = replace(outcome.row, intervention=True) if intervention else outcome.row
            store.record_run(row, path=db)
            rows[label].append(row)
            if on_run is not None:
                on_run(label, i, row)
    sec_total = time.monotonic() - t0

    arms = {label: _tally(rows[label]) for label in ARMS}
    return AbReport(
        verdict=decide_ab(arms["a"], arms["b"], min_n=min_n),
        arm_a=arms["a"],
        arm_b=arms["b"],
        rows_a=tuple(rows["a"]),
        rows_b=tuple(rows["b"]),
        sec_total=sec_total,
    )


def _spec(arm: ArmSpec | Selection) -> ArmSpec:
    """Aceita a `Selection` do router ou o `ArmSpec` cru: o A/B só precisa de
    backend e modelo, o resto é carona para o ledger."""
    if isinstance(arm, ArmSpec):
        return arm
    return ArmSpec(
        backend=arm.backend,
        model=getattr(arm, "model", None),
        tier=getattr(arm, "tier", None),
        max_turns=getattr(arm, "max_turns", None),
    )


def _tally(rows: list[RunRow]) -> Arm:
    return Arm(succ=sum(1 for r in rows if r.ok), n=len(rows))
