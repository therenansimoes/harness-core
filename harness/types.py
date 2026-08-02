"""Tipos do núcleo. Fonte única — todo módulo importa daqui, nada é redefinido."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Kind = Literal["code", "content", "config", "refactor", "infra"]

ExitReason = Literal["done", "max_turns", "timeout", "error", "blocked"]


@dataclass(frozen=True)
class UnitSpec:
    """Unidade de trabalho: o que fazer, onde, e como provar que ficou pronto."""

    id: str
    path: Path
    prompt: str
    verify_cmd: str
    kind: Kind | None = None


@dataclass(frozen=True)
class Capabilities:
    """O que um backend sabe fazer. Determinístico, sem I/O."""

    resumable: bool
    reports_cost: bool
    model_selectable: bool
    tools: frozenset[str]
    streaming: bool


@dataclass(frozen=True)
class Preflight:
    """Resultado de checagem local de um backend. ZERO chamada de LLM."""

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class ExecRequest:
    prompt: str
    workspace: Path
    tools: tuple[str, ...] = ()
    model: str | None = None
    max_turns: int = 1
    timeout_s: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)
    session_id: str | None = None
    trace_path: Path = Path("trace.jsonl")


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    exit_reason: ExitReason
    turns: int
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    files_changed: tuple[str, ...]
    session_id: str | None
    trace_path: Path


@dataclass(frozen=True)
class Selection:
    """Escolha do router: quem executa, com que modelo, em que classe de custo."""

    backend: str
    model: str
    tier: str
    kind: Kind
    max_turns: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """Saída do verify — a régua, não o agente, decide se passou."""

    passed: bool
    exit_code: int
    log_path: Path
    sec: float


@dataclass(frozen=True)
class RunRow:
    """Uma linha do ledger. `backend` e `kind` existem desde a linha 1."""

    run_id: str
    unit_id: str
    project: str | None
    backend: str
    model: str | None
    tier: str | None
    kind: str | None
    ok: bool
    exit_reason: str
    sec_total: float
    sec_provision: float
    cost_usd: float | None
    intervention: bool
    created_at: str
    id: int | None = None
