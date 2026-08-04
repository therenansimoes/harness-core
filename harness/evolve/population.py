"""Evolução populacional de configs — seleção por Wilson lower bound.

`evaluate` é injetado (nos testes é determinístico; na vida real vem do A/B do
harness). Nada aqui chama backend. `rng` sempre injetado: determinismo em teste.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from harness.ruler.wilson import wilson_interval

# Fração da população preservada intacta por geração (elitismo).
ELITE_FRAC = 0.25
# Perturbação relativa máxima em valores numéricos.
MUT_SCALE = 0.2


@dataclass(frozen=True)
class Individual:
    config: dict
    successes: int
    trials: int
    wilson_low: float


def mutate_config(base: dict, rng: random.Random) -> dict:
    """Perturbação pequena: números ganham ruído relativo; bool pode flipar;
    o resto fica como está. Nunca muta o dict de entrada."""
    out = {}
    for k, v in base.items():
        if isinstance(v, bool):
            out[k] = (not v) if rng.random() < 0.2 else v
        elif isinstance(v, int):
            delta = max(1, abs(int(v * MUT_SCALE)))
            out[k] = v + rng.randint(-delta, delta)
        elif isinstance(v, float):
            out[k] = v * (1.0 + rng.uniform(-MUT_SCALE, MUT_SCALE))
        elif isinstance(v, dict):
            out[k] = mutate_config(v, rng)
        else:
            out[k] = v
    return out


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    """Mistura por chave: cada chave vem de a ou b (moeda). União das chaves."""
    out = {}
    for k in {**a, **b}:
        if k not in a:
            out[k] = b[k]
        elif k not in b:
            out[k] = a[k]
        else:
            out[k] = a[k] if rng.random() < 0.5 else b[k]
    return out


def _score(config: dict, evaluate: Callable[[dict], tuple[int, int]]) -> Individual:
    succ, n = evaluate(config)
    low, _ = wilson_interval(succ, n)
    return Individual(config=config, successes=succ, trials=n, wilson_low=low)


def run_population(
    evaluate: Callable[[dict], tuple[int, int]],
    seeds: list[dict],
    generations: int,
    pop_size: int,
    rng: random.Random,
) -> list[Individual]:
    """PBT mínimo: avalia, ordena por wilson_low, elite passa, resto nasce de
    crossover+mutação entre sobreviventes. Devolve a última geração ordenada
    (melhor primeiro)."""
    if not seeds:
        raise ValueError("seeds vazio")
    pop = [_score(dict(c), evaluate) for c in seeds]
    for _ in range(generations):
        pop.sort(key=lambda i: i.wilson_low, reverse=True)
        n_elite = max(1, int(pop_size * ELITE_FRAC))
        elite = pop[:n_elite]
        parents = pop[: max(2, n_elite)]
        children = []
        while len(elite) + len(children) < pop_size:
            pa, pb = rng.sample(parents, 2) if len(parents) >= 2 else (parents[0], parents[0])
            child = mutate_config(crossover(pa.config, pb.config, rng), rng)
            children.append(_score(child, evaluate))
        pop = elite + children
    pop.sort(key=lambda i: i.wilson_low, reverse=True)
    return pop
