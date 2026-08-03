"""Governor: o CHEFE do harness — prazo, pressão e foco do loop.

Três alavancas, todas caindo sobre quem gasta:

- deadline: `check_deadline` corta o run quando o wall clock passa do teto.
- pressão: `taper_turns` reduz o max_turns a cada tentativa — resultado não
  veio, a corda encurta.
- foco: `explore_budget` derrete a fração de exploração conforme o prazo se
  aproxima (perto do fim só explota); `bench` tira do jogo ação que propõe
  muito e não emplaca KEEP.

Tudo função PURA com `now`/`ts` injetados: testável sem dormir. `load_gov`
falha aberta campo a campo, como `load_policy` do grafo — config ausente ou
torta degrada para os defaults congelados aqui, nunca derruba o run.

Mutação em `config/governor.toml` passa pelo meta-exame (`improve/meta.py`):
o loop não afrouxa o próprio prazo.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from harness.routing import config_dir

GOVERNOR_TOML = "governor.toml"

DeadlineStatus = Literal["continue", "cutoff"]
CONTINUE: DeadlineStatus = "continue"
CUTOFF: DeadlineStatus = "cutoff"


@dataclass(frozen=True)
class Governor:
    """Defaults congelados — o comportamento de fábrica quando o toml falta.

    `turn_taper=1.0` de propósito: sem config, pressão zero — o comportamento
    atual do grafo fica intacto (fail-open de verdade)."""

    run_s: float = 900.0
    cycle_s: float = 3600.0
    cost_cap_usd: float = 5.0
    turn_taper: float = 1.0
    explore_frac_start: float = 0.5
    explore_frac_end: float = 0.0
    bench_after: int = 3
    bench_cycles: int = 2


def _pos_float(raw: Any, default: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _frac(raw: Any, default: float) -> float:
    """Fração válida em [0, 1]; fora disso cai no default."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if 0.0 <= v <= 1.0 else default


def _pos_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def load_gov(path: Path | None = None) -> Governor:
    """`config/governor.toml` -> Governor. Falha aberta campo a campo."""
    p = Path(path) if path is not None else config_dir() / GOVERNOR_TOML
    base = Governor()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return base

    def sec(name: str) -> dict:
        s = data.get(name)
        return s if isinstance(s, dict) else {}

    deadline, pressure, focus = sec("deadline"), sec("pressure"), sec("focus")
    return Governor(
        run_s=_pos_float(deadline.get("run_s"), base.run_s),
        cycle_s=_pos_float(deadline.get("cycle_s"), base.cycle_s),
        cost_cap_usd=_pos_float(pressure.get("cost_cap_usd"), base.cost_cap_usd),
        turn_taper=_frac(pressure.get("turn_taper"), base.turn_taper),
        explore_frac_start=_frac(
            focus.get("explore_frac_start"), base.explore_frac_start
        ),
        explore_frac_end=_frac(focus.get("explore_frac_end"), base.explore_frac_end),
        bench_after=_pos_int(focus.get("bench_after"), base.bench_after),
        bench_cycles=_pos_int(focus.get("bench_cycles"), base.bench_cycles),
    )


def check_deadline(started_ts: float, now: float, gov: Governor) -> DeadlineStatus:
    """No limiar exato (elapsed == run_s) já é cutoff: prazo é prazo."""
    return CUTOFF if (now - started_ts) >= gov.run_s else CONTINUE


def taper_turns(base_turns: int, attempt: int, gov: Governor) -> int:
    """max_turns da tentativa: base * taper^attempt, nunca abaixo de 1."""
    tapered = int(base_turns * (gov.turn_taper ** max(0, attempt)))
    return max(1, tapered)


def explore_budget(elapsed_frac: float, gov: Governor) -> float:
    """Fração de exploração no instante `elapsed_frac` (0=início, 1=prazo).

    Interpolação linear start->end, clampada em [0, 1] dos dois lados —
    contrato para o bandit consumir depois."""
    f = min(1.0, max(0.0, float(elapsed_frac)))
    v = gov.explore_frac_start + (gov.explore_frac_end - gov.explore_frac_start) * f
    return min(1.0, max(0.0, v))


def bench(action_stats: Mapping[str, Mapping[str, Any]], gov: Governor) -> set[str]:
    """Ações banidas: >= bench_after propostas recentes e nenhum KEEP.

    `action_stats`: nome -> {"proposals": int, "keeps": int}. Stat torta ou
    incompleta conta como zero — ninguém vai pro banco por dado quebrado."""
    banned: set[str] = set()
    for name, stats in action_stats.items():
        if not isinstance(stats, Mapping):
            continue
        proposals = _pos_int(stats.get("proposals"), 0)
        keeps = _pos_int(stats.get("keeps"), 0)
        if proposals >= gov.bench_after and keeps == 0:
            banned.add(name)
    return banned
