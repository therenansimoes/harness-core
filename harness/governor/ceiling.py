"""Teto de gasto EXPLÍCITO, por comando — não confundir com `pressure.cost_cap_usd`.

`pressure.cost_cap_usd` (governor.py) é AMBIENTE: um teto configurado uma vez
em `config/governor.toml`, checado no gate depois que o dinheiro já saiu
(fail-open — sem config, sem corte, o run de sempre segue intocado). `Ceiling`
aqui é o OPOSTO nos dois eixos: é o `--max-usd` de UM pedido (`harness do`),
checado ANTES de despachar cada tentativa (fail-closed — gasto ilegível ou
backend que não reporta custo, com teto ativo, barram por precaução em vez de
arriscar estourar sem saber). Ver AGENTS.md §"Fail-open vs fail-closed": isto
é permissão para gastar, então erra fechado.

Os dois mecanismos coexistem sem se tocar — nenhum dos dois lê o outro.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.ledger import store

BUDGET_EXIT = "budget"  # ExitReason do execute barrado pelo teto
BREACH_REASON = "ceiling:teto_de_gasto"
BLIND_REASON = "ceiling:gasto_ilegivel"
NO_COST_REASON = "ceiling:backend_sem_custo"


@dataclass(frozen=True)
class Ceiling:
    """`limit_usd <= 0` = INATIVO — comportamento de hoje, byte a byte.

    `cmd_do` é o único gate que impede um valor não-positivo de chegar aqui
    fora do caso "flag ausente" (que vira `None` antes de `_ceiling`, não 0
    nem negativo).
    """

    limit_usd: float
    # Gasto de run_ids ANTERIORES do MESMO comando (`harness do`, escalada de
    # tier): None = DESCONHECIDO, e desconhecido com teto ativo é BLIND, não
    # zero — inventar 0.0 deixaria o teto furar exatamente no caso em que
    # menos se sabe do gasto real.
    prior_usd: float | None = 0.0

    @property
    def active(self) -> bool:
        return self.limit_usd > 0


@dataclass(frozen=True)
class Breach:
    fired: bool
    reason: str = ""
    spent_usd: float = 0.0
    limit_usd: float = 0.0


NO_BREACH = Breach(False)


def spent_for_run(run_id: str, db: Path, through_attempt: int) -> float:
    """Soma `cost_usd` de todos os `execute` de `0` até `through_attempt`, do
    ledger. `through_attempt < 0` = nenhuma tentativa ainda -> `0.0`, sem
    tocar o banco. Custo torto ou ausente PROPAGA (quem converte é `check`)."""
    if through_attempt < 0:
        return 0.0
    total = 0.0
    for a in range(through_attempt + 1):
        payload = store.get_node(run_id, "execute", db, attempt=a)
        total += float((payload or {}).get("cost_usd") or 0.0)
    return total


def check(
    ceiling: Ceiling, run_id: str, db: Path, attempt: int, backend_reports_cost: bool
) -> Breach:
    """Chamado ANTES de despachar `attempt`. NUNCA levanta — gasto ilegível
    vira `Breach` (fail-closed), não exceção pro chamador tratar."""
    if not ceiling.active:
        return NO_BREACH
    if ceiling.prior_usd is None:
        return Breach(True, BLIND_REASON, 0.0, ceiling.limit_usd)
    if not backend_reports_cost:
        return Breach(True, NO_COST_REASON, 0.0, ceiling.limit_usd)
    try:
        spent = ceiling.prior_usd + spent_for_run(run_id, db, attempt - 1)
    except Exception:
        return Breach(True, BLIND_REASON, 0.0, ceiling.limit_usd)
    if spent >= ceiling.limit_usd:
        return Breach(True, BREACH_REASON, spent, ceiling.limit_usd)
    return NO_BREACH
