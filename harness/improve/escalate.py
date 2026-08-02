"""Escalação pro humano: o payload do `interrupt()` e a taxa de intervenção.

O grafo não sabe conversar com gente — ele para. O que atravessa a parada é
este dicionário, e ele tem que bastar sozinho: quem lê está fora do processo,
possivelmente dias depois, sem o estado na cabeça. Daí os quatro campos serem
POR QUÊ (`reason`), SOBRE O QUÊ (`unit`), O QUE IA MUDAR (`mutation`) e COM QUE
BASE (`evidence`).

`intervention_rate` é o KPI do próprio loop: a promessa do PR-9 é "20 minutos
sem intervenção", e a única forma de saber se a promessa foi cumprida é contar
quantas runs precisaram de humano. Métrica de autonomia mora do lado de fora do
que ela mede — por isso ela lê o ledger, não o estado do grafo.
"""

from __future__ import annotations

from typing import Any, Sequence

from harness.types import RunRow

# Vocabulário fechado dos motivos. Texto livre aqui viraria motivo diferente a
# cada nó e ninguém conseguiria agrupar escalação por causa.
NO_GRADIENT = "no_gradient"           # pick_target não achou o que valha a pena
GENOME_VIOLATION = "genome_violation" # a regra escolhida toca zona proibida
DEADLINE = "deadline"                 # estourou o orçamento de tempo do ciclo
ERROR = "error"                       # falha repetida/inesperada aplicando a regra

REASONS = (NO_GRADIENT, GENOME_VIOLATION, DEADLINE, ERROR)

# O que o humano pode responder no `Command(resume=...)`.
CONTINUE = "continue"
ABORT = "abort"

DEFAULT_WINDOW = 50


def payload(
    reason: str,
    unit: str | Sequence[str] | None = None,
    mutation: dict | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict:
    """Monta o dicionário do interrupt. Motivo fora do vocabulário é bug nosso."""
    if reason not in REASONS:
        raise ValueError(f"motivo desconhecido: {reason!r} (use um de {REASONS})")
    units = [unit] if isinstance(unit, str) else list(unit or [])
    return {
        "reason": reason,
        "unit": units,
        "mutation": mutation,
        "evidence": dict(evidence or {}),
    }


def intervention_rate(
    history: Sequence[RunRow], window: int = DEFAULT_WINDOW
) -> float:
    """Fração das runs recentes que precisaram de humano. Sem run, 0.0.

    Zero por falta de amostra e zero por autonomia são a mesma leitura aqui de
    propósito: a taxa sozinha nunca é evidência, quem a publica publica o N
    junto (é o que o relatório do `harness improve` faz).
    """
    rows = list(history)[: max(0, int(window))]
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.intervention) / len(rows)
