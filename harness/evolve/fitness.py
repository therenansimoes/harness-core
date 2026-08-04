"""Fitness real da população: o indivíduo É um braço de execução, e a nota é a
do gate.

O genoma de um indivíduo é o kwargs do braço (`ab.ArmSpec`): `backend`/`model`
são strings/None — `mutate_config` passa por elas sem tocar — e `max_turns` é o
único knob numérico. A mutação da população, então, só mexe no que muda a
execução de verdade, e a evolução não troca de executor no meio do experimento
(trocar backend por moeda faria o wilson_low medir provedor, não config).

Duas escolhas herdadas do A/B (`harness/ab.py`), de propósito:

1. **Sucesso = `RunRow.ok`**, a decisão do gate, não o "terminei" do executor.
   Evolução e A/B contam a mesma coisa; se contassem diferente, o elite do
   archive não valeria como prior de nada.
2. **Toda run entra no ledger.** A evolução paga duas vezes: dá a nota agora e
   vira histórico do router depois.

Nada aqui escolhe backend por default — quem chama passa. O `harness evolve`
default é `mock` porque é o laço que roda mais vezes.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from harness.ab import ArmSpec
from harness.evolve.archive import COST_BUCKETS, Archive
from harness.evolve.population import Individual, mutate_config, run_population
from harness.ledger import store
from harness.types import RunRow, UnitSpec

# Braço com zero turno não executa: mutação que zera (ou negativa) `max_turns`
# viraria uma população de indivíduos que falham por construção.
MIN_TURNS = 1
# Fronteiras de custo POR RUN (USD) dos nichos do MAP-Elites. Mock custa 0 e
# cai em 'low': o nicho barato é o que a evolução determinística preenche.
COST_LOW = 0.05
COST_MID = 0.50


def config_key(config: dict) -> str:
    """Chave canônica do genoma — é como a nota volta a achar o indivíduo."""
    return json.dumps(config, sort_keys=True, default=str)


def arm_spec(config: dict) -> ArmSpec:
    """Genoma -> braço. Único lugar que sabe o mapeamento; o clamp de turnos
    mora aqui e não no `mutate_config` (a população não conhece semântica)."""
    return ArmSpec(
        backend=str(config["backend"]),
        model=config.get("model"),
        tier=config.get("tier"),
        max_turns=max(MIN_TURNS, int(config.get("max_turns", MIN_TURNS))),
    )


def cost_bucket(cost_per_run: float) -> str:
    if cost_per_run < COST_LOW:
        return COST_BUCKETS[0]
    if cost_per_run < COST_MID:
        return COST_BUCKETS[1]
    return COST_BUCKETS[2]


@dataclass(frozen=True)
class EvalStats:
    """O que a run rendeu além de (sucessos, tentativas): é daqui que sai o
    nicho do archive, que a assinatura de `evaluate` não tem como devolver."""

    succ: int
    n: int
    cost_usd: float
    kind: str

    @property
    def cost_per_run(self) -> float:
        return self.cost_usd / self.n if self.n else 0.0


@dataclass
class Fitness:
    """`evaluate` de verdade: roda a unidade `n` vezes por genoma e conta o gate.

    Callable — entra em `run_population` no lugar do fake dos testes. Guarda
    `stats` por genoma para quem precisa do nicho (custo/kind) depois.
    """

    units: list[UnitSpec]
    n: int = 1
    data_dir: Path | str | None = None
    project: str | None = None
    on_run: object | None = None  # Callable[[str, int, RunRow], None] | None
    stats: dict[str, EvalStats] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.units:
            raise ValueError("fitness sem unidade: não há o que medir")
        if self.n <= 0:
            raise ValueError(f"n tem que ser positivo: {self.n}")

    @property
    def kind(self) -> str:
        """Kind do nicho. Mais de um kind na avaliação = 'mixed': dizer que o
        elite é de um deles seria mentir para o router."""
        kinds = {u.kind or "code" for u in self.units}
        return kinds.pop() if len(kinds) == 1 else "mixed"

    def __call__(self, config: dict) -> tuple[int, int]:
        # Import tardio: o cli chama a evolução, então a evolução não importa o
        # cli no topo (ciclo) — mesmo padrão do ab.py.
        from harness.cli import run_once

        spec = arm_spec(config)
        db = Path(self.data_dir) / store.DB_NAME if self.data_dir is not None else None
        succ = 0
        total = 0
        cost = 0.0
        for unit in self.units:
            for i in range(1, self.n + 1):
                outcome = run_once(
                    unit,
                    spec.backend,
                    spec.model,
                    project=self.project,
                    tier=spec.tier,
                    max_turns=spec.max_turns or MIN_TURNS,
                )
                row: RunRow = outcome.row
                store.record_run(row, path=db)
                succ += 1 if row.ok else 0
                total += 1
                cost += row.cost_usd or 0.0
                if self.on_run is not None:
                    self.on_run(unit.id, i, row)  # type: ignore[operator]
        self.stats[config_key(config)] = EvalStats(succ, total, cost, self.kind)
        return succ, total


@dataclass(frozen=True)
class EvolveReport:
    best: Individual
    population: tuple[Individual, ...]
    steps: int
    elites: tuple[tuple[str, str], ...]  # nichos que ESTA rodada tomou


def seed_configs(base: dict, pop_size: int, rng: random.Random) -> list[dict]:
    """Semente = o base intacto + mutações dele. O base entra sem ruído porque
    a evolução tem que poder perder: sem baseline na população, um passo ruim
    não tem como voltar."""
    if pop_size < 1:
        raise ValueError(f"pop_size tem que ser >= 1: {pop_size}")
    seeds = [dict(base)]
    while len(seeds) < pop_size:
        seeds.append(mutate_config(base, rng))
    return seeds


def evolve(
    evaluate,
    base: dict,
    archive: Archive,
    *,
    steps: int = 1,
    pop_size: int = 4,
    seed: int = 0,
) -> EvolveReport:
    """`steps` gerações de PBT sobre `base` e o melhor de cada nicho no archive.

    Recebe `seed` (int) e não o `random.Random`: quem chama é a CLI, e o
    determinismo tem que caber numa flag. `evaluate` continua injetado — o fake
    dos testes entra aqui igual ao `Fitness` real. Se ele expõe `.stats`
    (o `Fitness` expõe), o nicho sai do custo medido; senão cai no barato, que
    é o que um evaluate sem custo de fato é.
    """
    if steps < 1:
        raise ValueError(f"steps tem que ser >= 1: {steps}")
    rng = random.Random(seed)
    seeds = seed_configs(base, pop_size, rng)
    pop = run_population(evaluate, seeds, steps, pop_size, rng)
    stats: dict[str, EvalStats] = getattr(evaluate, "stats", {})
    taken: list[tuple[str, str]] = []
    for ind in pop:
        st = stats.get(config_key(ind.config))
        niche = (
            (st.kind, cost_bucket(st.cost_per_run))
            if st is not None
            else (getattr(evaluate, "kind", "code"), COST_BUCKETS[0])
        )
        if archive.add(niche, ind.config, ind.wilson_low):
            taken.append(niche)
    return EvolveReport(best=pop[0], population=tuple(pop), steps=steps, elites=tuple(taken))
