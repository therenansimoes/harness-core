"""Guards: o freio do reorg — orçamento, parada atada ao verify e trava de
oscilação. Tudo falha ABERTO e vira linha no ledger; guard nunca derruba um
run, só para de escalar.

Três guards, todos sobre sinal que já está no ledger:

    budget       gasto/tentativa/relógio do run passou do teto -> parar de
                 ESCALAR (sem subir tier, sem revisor); o run segue no piso
    verify_stop  o verify reprovou N vezes seguidas no tier de cima -> não há
                 para onde escalar, tentar de novo é queimar orçamento
    freeze       a mesma regra de reorg aplicada -> revertida -> aplicada é
                 oscilação: a topologia congela pelo resto do run

Núcleo PURO, mesmo desenho do `reorg`: as funções daqui não leem disco nem
relógio — o sinal entra pronto, injetado por quem conhece o ledger
(`run_graph`). A única exceção é `load_guards`, que lê o toml e degrada campo a
campo para os defaults congelados aqui. Nenhuma função levanta por dado ruim.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.routing import config_dir

GOVERNOR_TOML = "governor.toml"
SECTION = "guards"

# Prefixo dos `rule_id` de guard no nó do reorg: é o que deixa o diff do reorg
# ignorar estas linhas — guard nunca aparece como "revertido".
GUARD_PREFIX = "guard:"
G_BUDGET = "guard:budget"
G_VERIFY = "guard:verify_stop"
G_FREEZE = "guard:freeze"

# `action` das linhas de guard no ledger, vocabulário fechado como o do reorg.
A_BUDGET = "stop_escalation"
A_VERIFY = "stop_retry"
A_FREEZE = "freeze_topology"

# Estados das linhas de reorg que o flip-flop examina (== reorg.STATE_ACTIVE /
# reorg.STATE_REVERTED; literais aqui para o módulo continuar autossuficiente).
_APPLIED = "applied"
_REVERTED = "reverted"


@dataclass(frozen=True)
class GuardsConfig:
    """Defaults congelados — o comportamento de fábrica sem `[guards]` no toml.

    Tetos folgados de propósito: guard de fábrica só fala quando o run já
    gastou de verdade. Zero desliga o teto individual; `enabled=False` desliga
    tudo."""

    enabled: bool = True
    max_cost_usd: float = 3.0
    max_attempts: int = 4
    max_wall_s: float = 600.0
    verify_fail_stop: int = 3
    flipflop_window: int = 6


@dataclass(frozen=True)
class GuardVerdict:
    """Um guard falou (ou não). `signal` é a evidência, como no reorg —
    verdict sem o número que o causou não é auditável."""

    fired: bool
    guard_id: str = ""
    reason: str = ""
    signal: dict = field(default_factory=dict)


NONE_FIRED = GuardVerdict(fired=False)


def _bool(raw: Any, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _cap_float(raw: Any, default: float) -> float:
    """Teto em que 0 é resposta válida: "desligado". Bool, ilegível ou negativo
    é torto -> default. (Mesma semântica do `_cap_float` do governor,
    reimplementada aqui para o módulo não depender dele.)"""
    if isinstance(raw, bool):
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def _cap_int(raw: Any, default: int) -> int:
    """Teto inteiro em que 0 é "desligado"; bool/ilegível/negativo -> default."""
    if isinstance(raw, bool):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def _win_int(raw: Any, default: int) -> int:
    """Janela de linhas: menos de 2 não detecta ida e volta -> default."""
    if isinstance(raw, bool):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 2 else default


def load_guards(path: Path | None = None) -> GuardsConfig:
    """`[guards]` de `config/governor.toml` -> GuardsConfig. Falha aberta campo
    a campo: arquivo ausente, ilegível, sem a seção ou com tipo torto =
    defaults congelados."""
    p = Path(path) if path is not None else config_dir() / GOVERNOR_TOML
    base = GuardsConfig()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return base
    sec = data.get(SECTION)
    if not isinstance(sec, dict):
        return base
    return GuardsConfig(
        enabled=_bool(sec.get("enabled"), base.enabled),
        max_cost_usd=_cap_float(sec.get("max_cost_usd"), base.max_cost_usd),
        max_attempts=_cap_int(sec.get("max_attempts"), base.max_attempts),
        max_wall_s=_cap_float(sec.get("max_wall_s"), base.max_wall_s),
        verify_fail_stop=_cap_int(sec.get("verify_fail_stop"), base.verify_fail_stop),
        flipflop_window=_win_int(sec.get("flipflop_window"), base.flipflop_window),
    )


def _num(raw: Any, default: float = 0.0) -> float:
    if isinstance(raw, bool):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def budget_exceeded(
    spent_usd: Any, attempt: Any, elapsed_s: Any, cfg: GuardsConfig
) -> GuardVerdict:
    """O run ainda tem orçamento para ESCALAR? Ordem fixa (custo, tentativas,
    relógio): dois tetos estourados dão sempre o mesmo motivo. `>=` no teto
    exato, igual ao governor — teto é teto."""
    spent = _num(spent_usd)
    tent = int(_num(attempt))
    elapsed = _num(elapsed_s)
    if cfg.max_cost_usd > 0 and spent >= cfg.max_cost_usd:
        return GuardVerdict(
            True,
            G_BUDGET,
            f"guard:budget:cost:${spent:.4f}>=${cfg.max_cost_usd:.4f}",
            {"kind": "cost", "spent_usd": spent, "cap_usd": cfg.max_cost_usd},
        )
    if cfg.max_attempts > 0 and tent >= cfg.max_attempts:
        return GuardVerdict(
            True,
            G_BUDGET,
            f"guard:budget:attempts:{tent}>={cfg.max_attempts}",
            {"kind": "attempts", "attempt": tent, "cap": cfg.max_attempts},
        )
    if cfg.max_wall_s > 0 and elapsed >= cfg.max_wall_s:
        return GuardVerdict(
            True,
            G_BUDGET,
            f"guard:budget:wall:{elapsed:.1f}s>={cfg.max_wall_s:.1f}s",
            {"kind": "wall", "elapsed_s": elapsed, "cap_s": cfg.max_wall_s},
        )
    return NONE_FIRED


def verify_stop(consecutive_top_fails: Any, cfg: GuardsConfig) -> GuardVerdict:
    """N verifies vermelhos SEGUIDOS no tier de cima: não há degrau acima e
    tentar de novo é pagar pelo mesmo vermelho. Contagem ilegível vira zero —
    o guard prefere silêncio a chute."""
    n = max(0, int(_num(consecutive_top_fails)))
    if cfg.verify_fail_stop > 0 and n >= cfg.verify_fail_stop:
        return GuardVerdict(
            True,
            G_VERIFY,
            f"guard:verify_stop:{n}x_top_tier",
            {"consecutive_fails": n, "cap": cfg.verify_fail_stop},
        )
    return NONE_FIRED


def flipflop(rows: Sequence[Mapping], cfg: GuardsConfig) -> GuardVerdict:
    """A mesma regra aplicada -> revertida -> aplicada dentro da janela é
    oscilação: o reorg está desfazendo a si mesmo e a topologia congela.

    `rows` são os payloads de reorg DESTE run, na ordem de gravação (saída de
    `reorg.active_from_ledger`). Linha de guard, linha torta e `rule_id` sem
    nome não contam; `state` ausente vale "applied", igual ao `diff_active`.
    Dispara pela PRIMEIRA regra (na ordem das linhas) que oscilou —
    determinístico."""
    try:
        window = list(rows)[-max(2, int(cfg.flipflop_window)) :]
    except Exception:
        return NONE_FIRED
    seqs: dict[str, int] = {}
    for row in window:
        if not isinstance(row, Mapping):
            continue
        rule_id = row.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id or rule_id.startswith(GUARD_PREFIX):
            continue
        state = row.get("state", _APPLIED)
        fase = seqs.setdefault(rule_id, 0)
        if fase == 0 and state == _APPLIED:
            seqs[rule_id] = 1
        elif fase == 1 and state == _REVERTED:
            seqs[rule_id] = 2
        elif fase == 2 and state == _APPLIED:
            return GuardVerdict(
                True,
                G_FREEZE,
                f"guard:freeze:{rule_id}",
                {"rule_id": rule_id, "window": len(window)},
            )
    return NONE_FIRED


def frozen(rows: Sequence[Mapping]) -> bool:
    """A topologia deste run está congelada? Freeze vale até o fim do run — a
    linha nunca é revertida, então basta existir."""
    return any(isinstance(r, Mapping) and r.get("rule_id") == G_FREEZE for r in rows)
