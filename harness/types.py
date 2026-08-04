"""Tipos do núcleo. Fonte única — todo módulo importa daqui, nada é redefinido."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Kind = Literal["code", "content", "config", "refactor", "infra"]

# "stalled": executou, não escreveu nada e não disse nada.
# "truncated": a última resposta do modelo morreu no teto de tokens
# (finish_reason=length) — braço CORTADO, não braço ruim. Sem isto o A/B e o
# bandit contam como "done" (ou "stalled") um run que nem chegou a terminar a
# frase, e punem/premiam o braço pelo motivo errado.
ExitReason = Literal["done", "max_turns", "timeout", "error", "blocked", "stalled", "truncated"]


@dataclass(frozen=True)
class Check:
    """Um item da régua graduada, nomeado e com peso.

    O `verify_cmd` continua sendo a régua binária; os checks nomeados dizem
    QUANTO da régua passou, o que dá ao retry um alvo em vez de um "reprovou".
    `weight` é relativo — só a proporção importa.
    """

    name: str
    cmd: str
    weight: float = 1.0


@dataclass(frozen=True)
class UnitSpec:
    """Unidade de trabalho: o que fazer, onde, e como provar que ficou pronto."""

    id: str
    path: Path
    prompt: str
    verify_cmd: str
    kind: Kind | None = None
    # Projeto real (config/projects.toml): o run acontece num git worktree do
    # repo do projeto (branch efêmera `harness/<run_id>`), e o accept entrega
    # a branch `harness/<unit_id>` para review humano. `None` = workspace
    # comum, o caminho default.
    project: str | None = None
    # `[checks]` do unit.toml: ADITIVO. Vazio (o default) = régua binária de
    # sempre, byte a byte.
    checks: tuple[Check, ...] = ()


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
    run_id: str | None = None
    # Roteamento decidiu o kind; sem ele no request o backend só acha skill de
    # `kinds = []` — nenhuma seed tem — e a injeção nunca acontece.
    kind: str | None = None


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
    # Régua graduada: fração do peso que passou (1.0 quando não há `[checks]`,
    # que é o caso de toda unidade escrita antes disto) e nomes dos reprovados.
    # Defaults obrigatórios — os call sites posicionais já existentes não sabem
    # destes campos.
    score: float = 1.0
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class MutationRow:
    """Uma mutação de config avaliada pelo loop de melhoria.

    `arm_a`/`arm_b` são `"sucessos/tentativas"` — o mesmo formato de `harness ab
    --a 5/6`, legível no sqlite e parseável sem esquema extra. `verdict` é o da
    régua (KEEP/DISCARD/INCONCLUSIVE), `REJECTED`, quando o genoma barrou a
    regra antes de qualquer run, ou `ABORTED`, quando o experimento começou e
    não terminou. `note` carrega a violação ou o motivo da escalação: rejeição
    sem causa registrada não é auditável. `action` é a ação de evolução que
    propôs a mutação, em coluna própria: placar por ação não pode depender de
    parsear texto livre. None nas linhas gravadas antes da coluna existir e nas
    mutações que não vieram de uma ação.
    """

    mutation_id: str
    rule_id: str
    verdict: str
    arm_a: str
    arm_b: str
    applied_at: str
    reverted: bool
    note: str | None = None
    action: str | None = None


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
