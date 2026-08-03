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
from harness.ruler import pareto
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
    # Eixos do Pareto (custo/tempo médios por run, por braço) e os eixos em que
    # B regrediu — vazio quando `[pareto]` está desligado no ruler.toml.
    axes_a: dict
    axes_b: dict
    pareto_worse: tuple[str, ...] = ()


def run_ab(
    unit_dir: Path | str,
    arm_a: ArmSpec | Selection | None = None,
    arm_b: ArmSpec | Selection | None = None,
    n: int = MIN_N,
    data_dir: Path | str | None = None,
    *,
    min_n: int = MIN_N,
    project: str | None = None,
    on_run: Callable[[str, int, RunRow], None] | None = None,
    before_run: Callable[[str, int], None] | None = None,
    spec_of: Callable[[str], ArmSpec] | None = None,
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

    `spec_of` recebe o rótulo do braço e devolve o `ArmSpec` daquela run, e tem
    PRECEDÊNCIA sobre `arm_a`/`arm_b` (que continuam valendo para o braço
    estático — `harness ab --dim backend`). Ele é avaliado DEPOIS do
    `before_run`, e a ordem é o experimento inteiro: quem monta o braço a
    partir da config (o autopilot) precisa ler a config JÁ com a mutação
    ligada/desligada. Invertido, o braço B nasceria montado com a config do
    baseline e os dois braços mediriam a mesma coisa.

    `intervention=True` marca as linhas do ledger como "teve humano no loop".
    Quem sabe disso é o chamador (o grafo que foi retomado por um resume), não
    a run.
    """
    # Import tardio: o cli é quem chama o A/B, então o A/B não importa o cli no
    # topo (ciclo) — mesmo padrão do graph/run_graph.py.
    from harness.cli import DEFAULT_MAX_TURNS, load_unit, run_once

    if n <= 0:
        raise ValueError(f"n tem que ser positivo: {n}")
    if spec_of is None and (arm_a is None or arm_b is None):
        raise ValueError("A/B sem braço: passe arm_a/arm_b ou spec_of")

    unit = load_unit(Path(unit_dir))
    if spec_of is None:
        # Braço estático: o mesmo `ArmSpec` em toda run daquele rótulo. Vira
        # `spec_of` aqui para o laço ter um caminho só.
        static = dict(zip(ARMS, (_spec(arm_a), _spec(arm_b))))
        spec_of = static.__getitem__
    db = Path(data_dir) / store.DB_NAME if data_dir is not None else None
    rows: dict[str, list[RunRow]] = {label: [] for label in ARMS}

    t0 = time.monotonic()
    for i in range(1, n + 1):
        for label in ARMS:
            if before_run is not None:
                before_run(label, i)
            spec = spec_of(label)
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
    axes = {label: _axes(rows[label]) for label in ARMS}
    verdict, worse = pareto.apply(
        decide_ab(arms["a"], arms["b"], min_n=min_n),
        axes["a"],
        axes["b"],
        pareto.load_pareto(),
    )
    return AbReport(
        verdict=verdict,
        arm_a=arms["a"],
        arm_b=arms["b"],
        rows_a=tuple(rows["a"]),
        rows_b=tuple(rows["b"]),
        sec_total=sec_total,
        axes_a=axes["a"],
        axes_b=axes["b"],
        pareto_worse=tuple(worse),
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


def _axes(rows: list[RunRow]) -> dict:
    """Média POR RUN de cada eixo do Pareto.

    `cost_usd` sai do numerador E do denominador quando a run não mediu (mock
    não cobra): média de custo diluída por runs de $0 diria que o braço é
    barato. `None` quando nenhuma run mediu — o Pareto trata isso como eixo
    ausente e não bloqueia.
    """
    costs = [r.cost_usd for r in rows if r.cost_usd is not None]
    return {
        "cost_usd": sum(costs) / len(costs) if costs else None,
        "sec_total": sum(r.sec_total for r in rows) / len(rows) if rows else None,
    }


def _tally(rows: list[RunRow]) -> Arm:
    return Arm(succ=sum(1 for r in rows if r.ok), n=len(rows))
