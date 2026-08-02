"""Piso do intervalo de Wilson — cópia privada mínima.

Duplicado de `ruler.wilson` por independência de branch; unificar depois é
permitido pois `routing/` e `ruler/` são ambos immutable no genoma (nenhum
loop de auto-melhoria mexe em nenhum dos dois, então as duas cópias não podem
divergir por mutação — só por edição humana).
"""

from __future__ import annotations

import math

Z = 1.96  # ~95%


def wilson_lower_bound(succ: int, n: int, z: float = Z) -> float:
    """Wald (p ± z·sqrt(p(1-p)/n)) degenera justamente no caso que o prior vê:
    N pequeno e p colado em 0 ou 1 devolve largura zero e finge certeza. Wilson
    não degenera — 6/6 vira 0.61, não 1.0, e 0/6 vira 0.0, não "impossível"."""
    if n <= 0:
        return 0.0
    if not 0 <= succ <= n:
        raise ValueError(f"succ fora de [0, n]: succ={succ}, n={n}")
    p = succ / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, center - half)
