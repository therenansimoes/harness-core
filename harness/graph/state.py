"""Estado do run_graph. Só tipos do núcleo — nenhum import de vendor aqui."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from harness.types import ExecResult, Selection, UnitSpec, Verdict

Action = Literal["accept", "retry", "revert", "escalate_human"]

# Um evento é um dict achatado ({"node": ..., "at": ..., ...}) para atravessar
# o checkpointer sem tipo próprio. A lista é o trace do run.
Event = dict[str, Any]


@dataclass(frozen=True)
class Budget:
    """Teto do run. `deadline_ts` é epoch em segundos; PR-9 é quem mata por ele."""

    spent_usd: float = 0.0
    deadline_ts: float | None = None
    max_attempts: int = 2


@dataclass(frozen=True)
class Decision:
    """Saída do gate. PR-4 move isto para `ruler/gate.py` com a régua real."""

    action: Action
    reason: str = ""


class RunState(TypedDict):
    run_id: str
    unit: UnitSpec
    attempt: int
    selection: Selection | None
    workspace: str | None
    exec: ExecResult | None
    verdict: Verdict | None
    kpi_before: dict[str, float]
    kpi_after: dict[str, float]
    tamper: list[str]
    decision: Decision | None
    budget: Budget
    events: Annotated[list[Event], operator.add]
