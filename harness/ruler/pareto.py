"""Pareto: qualidade não é o único eixo. Custo e tempo também contam.

Wilson diz se B acerta mais que A. Não diz nada sobre B acertar o mesmo
gastando o triplo. Este módulo é o segundo filtro: um KEEP de Wilson que
regride custo ou tempo além da tolerância vira INCONCLUSIVE — não DISCARD,
porque a mutação PROVOU qualidade e quem decide o trade-off é o humano.

**Fail-open, ao contrário do `regressed` do KPI.** Lá, KPI que some no "after"
é regressão sempre: o número vem do projeto sob teste e ausência pode ser
fraude (deletar o teste que media). Aqui a medida é do próprio harness — custo
`None` é backend mock, que não cobra, e média zero é run que não gastou. Eixo
sem medida no baseline não tem contra o que comparar, então ele não bloqueia:
o pior que acontece é um KEEP que Wilson já tinha dado. Fechar aqui travaria
todo A/B de mock, que é justamente o caminho de $0 do repo.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from math import isnan
from pathlib import Path

from harness import paths
from harness.ruler.wilson import INCONCLUSIVE, KEEP, AbVerdict

# Os eixos, na ordem em que entram no motivo. Qualidade não está aqui: ela é o
# veredito de Wilson, que este módulo recebe pronto.
AXES = ("cost_usd", "sec_total")

# Mesmo arquivo (e mesma política) do gate: ausente/malformado => defaults
# congelados abaixo, que são o comportamento de hoje — Pareto DESLIGADO. E,
# como no gate, `RULER_CONFIG` é override: `None` deixa `harness.paths` decidir
# em call-time.
RULER_CONFIG: Path | None = None
PARETO_ENABLED = False
PARETO_COST_TOL = 0.10
PARETO_SEC_TOL = 0.10


@dataclass(frozen=True)
class ParetoConfig:
    """Tolerância RELATIVA de piora da média por run, por eixo."""

    enabled: bool
    cost_tolerance_pct: float
    sec_tolerance_pct: float


DEFAULT_CONFIG = ParetoConfig(PARETO_ENABLED, PARETO_COST_TOL, PARETO_SEC_TOL)


def _ruler_config() -> Path:
    return RULER_CONFIG or paths.config_file("ruler.toml")


def load_pareto(config_path: Path | None = None) -> ParetoConfig:
    """Lê `[pareto]` de `config/ruler.toml`.

    Qualquer falha (arquivo ausente, TOML inválido, valor não-numérico,
    negativo ou NaN) devolve o default congelado, campo por campo. `enabled`
    só liga se for exatamente `true` no toml: "sim", 1 ou "true" não ligam
    nada — filtro extra nunca entra por coerção de tipo.
    """
    path = _ruler_config() if config_path is None else config_path
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return DEFAULT_CONFIG
    raw = data.get("pareto") or {}
    if not isinstance(raw, Mapping):
        return DEFAULT_CONFIG
    return ParetoConfig(
        enabled=raw.get("enabled") is True,
        cost_tolerance_pct=_tol(raw.get("cost_tolerance_pct"), PARETO_COST_TOL),
        sec_tolerance_pct=_tol(raw.get("sec_tolerance_pct"), PARETO_SEC_TOL),
    )


def _tol(raw, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0.0 and not isnan(value) else default


def worse_axes(
    a: Mapping[str, float | None],
    b: Mapping[str, float | None],
    cfg: ParetoConfig = DEFAULT_CONFIG,
) -> list[str]:
    """Eixos em que B regrediu contra A além da tolerância, na ordem de `AXES`.

    Eixo sem baseline utilizável (`None`, NaN ou <= 0) ou sem medida em B sai
    da conta — ver o fail-open no docstring do módulo.
    """
    tols = {"cost_usd": cfg.cost_tolerance_pct, "sec_total": cfg.sec_tolerance_pct}
    worse = []
    for axis in AXES:
        base, cand = _num(a.get(axis)), _num(b.get(axis))
        if base is None or base <= 0.0 or cand is None:
            continue
        if cand > base * (1.0 + tols[axis]):
            worse.append(axis)
    return worse


def _num(raw) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if isnan(value) else value


def apply(
    verdict: AbVerdict,
    a: Mapping[str, float | None],
    b: Mapping[str, float | None],
    cfg: ParetoConfig = DEFAULT_CONFIG,
) -> tuple[AbVerdict, list[str]]:
    """Segundo filtro sobre o veredito de Wilson.

    Só mexe em KEEP: DISCARD e INCONCLUSIVE já não vão calibrar nada, e
    reescrevê-los só embaralharia o motivo no ledger.
    """
    if not cfg.enabled or verdict != KEEP:
        return verdict, []
    worse = worse_axes(a, b, cfg)
    if worse:
        return INCONCLUSIVE, worse
    return verdict, []
