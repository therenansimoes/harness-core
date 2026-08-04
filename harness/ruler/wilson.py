"""Intervalo de Wilson e veredito ternário de A/B — a régua estatística.

Wald (p ± z·sqrt(p(1-p)/n)) degenera justamente no caso que aparece aqui: N
pequeno e p colado em 0 ou 1 devolve intervalo de largura zero e finge certeza.
Wilson não degenera — 6/6 vira [0.61, 1.0], não [1.0, 1.0].
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Z = 1.96  # 95%
MIN_N = 6  # tentativas por braço para a régua opinar

AbVerdict = Literal["KEEP", "DISCARD", "INCONCLUSIVE"]
KEEP, DISCARD, INCONCLUSIVE = "KEEP", "DISCARD", "INCONCLUSIVE"


@dataclass(frozen=True)
class Arm:
    """Um braço do A/B: `succ` sucessos em `n` tentativas."""

    succ: int
    n: int


def wilson_interval(succ: int, n: int, z: float = Z) -> tuple[float, float]:
    """Intervalo de Wilson (score interval) para uma proporção.

    `n <= 0` devolve [0, 1]: sem dado, o intervalo é a ignorância inteira.
    """
    if n <= 0:
        return 0.0, 1.0
    p = succ / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, center - half), min(1.0, center + half)


def decide_ab(a: Arm, b: Arm, min_n: int = MIN_N) -> AbVerdict:
    """Veredito de B (candidata) contra A (baseline). A regra, em ordem:

    1. `n < min_n` em qualquer braço  -> INCONCLUSIVE (amostra não opina)
    2. `lo(B) > hi(A)`                -> KEEP     (não sobrepõe e B é melhor)
    3. `hi(B) < lo(A)`                -> DISCARD  (não sobrepõe e A é melhor)
    4. sobreposição                   -> INCONCLUSIVE

    Sobreposição é "não dá para distinguir com o N que existe". INCONCLUSIVE é
    resposta honesta, não empate a favor de B: nunca promove. Quem quiser
    veredito, rode mais tentativas.
    """
    if a.n < min_n or b.n < min_n:
        return INCONCLUSIVE
    lo_a, hi_a = wilson_interval(a.succ, a.n)
    lo_b, hi_b = wilson_interval(b.succ, b.n)
    if lo_b > hi_a:
        return KEEP
    if hi_b < lo_a:
        return DISCARD
    return INCONCLUSIVE
