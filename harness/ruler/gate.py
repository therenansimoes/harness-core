"""Gate: o combinador ÚNICO de decisão pós-run. Nenhum outro módulo decide.

A ordem das regras é a prioridade:

1. `tamper` não-vazio  -> revert   (mexeu no immutable; o resto não importa)
2. verify não passou   -> retry    (não provou que funciona)
3. KPI regrediu        -> revert   (passou nos testes e piorou o projeto)
4. senão               -> accept

Regra 3 é o aceite do PR-4: verde no verify não compra regressão de KPI.
`escalate_human` é ação válida do tipo, mas quem a emite é o router/improve —
o gate não escala sozinho.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from harness.ruler.kpi import KpiSpec, regressed
from harness.types import Verdict

Action = Literal["accept", "retry", "revert", "escalate_human"]

TAMPER_PREFIX = "tamper:"


@dataclass(frozen=True)
class Decision:
    """O que fazer com o run, e por quê — `reason` vai inteiro para o ledger."""

    action: Action
    reason: str


def gate(
    verdict: Verdict,
    kpi_before: Mapping[str, float],
    kpi_after: Mapping[str, float],
    tamper: Sequence[str],
    specs: Mapping[str, KpiSpec] | None = None,
) -> Decision:
    """Aplica as 4 regras acima e devolve a Decision."""
    if tamper:
        return Decision("revert", TAMPER_PREFIX + ",".join(_tamper_name(t) for t in tamper))
    if not verdict.passed:
        return Decision("retry", f"verify_failed:exit={verdict.exit_code}")
    worse = regressed(kpi_before, kpi_after, specs)
    if worse:
        return Decision("revert", "kpi_regression:" + ",".join(worse))
    return Decision("accept", "verify ok, sem regressão de KPI")


def _tamper_name(entry: str) -> str:
    """`tamper/detect` já devolve entradas prefixadas; não duplica o prefixo."""
    return entry[len(TAMPER_PREFIX):] if entry.startswith(TAMPER_PREFIX) else entry
