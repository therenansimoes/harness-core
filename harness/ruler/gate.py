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

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isnan, nan
from pathlib import Path
from typing import Literal

from harness.ruler.kpi import KpiSpec, regressed
from harness.types import Verdict

Action = Literal["accept", "retry", "revert", "escalate_human"]

TAMPER_PREFIX = "tamper:"

# Overrides operacionais do juiz. Defaults congelados AQUI: config/ruler.toml
# ausente/malformado/inválido => comportamento idêntico ao histórico. Mutação
# do toml só entra via meta-exame (harness/improve/meta.py) + ack humano.
RULER_CONFIG = Path(__file__).resolve().parents[2] / "config" / "ruler.toml"
DEFAULT_KPI_REGRESSION_TOLERANCE = 0.0


def kpi_regression_tolerance(config_path: Path | None = None) -> float:
    """Tolerância absoluta de piora de KPI vinda de `config/ruler.toml`.

    Qualquer falha (arquivo ausente, TOML inválido, valor não-numérico ou
    negativo) devolve o default congelado — o juiz nunca afrouxa por acidente.
    """
    path = RULER_CONFIG if config_path is None else config_path
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return DEFAULT_KPI_REGRESSION_TOLERANCE
    raw = (data.get("gate") or {}).get("kpi_regression_tolerance")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_KPI_REGRESSION_TOLERANCE
    return value if value >= 0.0 and not isnan(value) else DEFAULT_KPI_REGRESSION_TOLERANCE


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
    *,
    config_path: Path | None = None,
) -> Decision:
    """Aplica as 4 regras acima e devolve a Decision.

    `config_path` só existe para teste; produção lê `config/ruler.toml`.
    """
    if tamper:
        return Decision("revert", TAMPER_PREFIX + ",".join(_tamper_name(t) for t in tamper))
    if not verdict.passed:
        return Decision("retry", f"verify_failed:exit={verdict.exit_code}")
    worse = regressed(kpi_before, kpi_after, specs)
    tol = kpi_regression_tolerance(config_path)
    if tol > 0.0 and worse:
        # NaN/sumido fica: abs(nan - x) é NaN e NaN > tol é False — trata à parte.
        worse = [
            name
            for name in worse
            if isnan(new := float(kpi_after.get(name, nan)))
            or abs(new - float(kpi_before[name])) > tol
        ]
    if worse:
        return Decision("revert", "kpi_regression:" + ",".join(worse))
    return Decision("accept", "verify ok, sem regressão de KPI")


def _tamper_name(entry: str) -> str:
    """`tamper/detect` já devolve entradas prefixadas; não duplica o prefixo."""
    return entry[len(TAMPER_PREFIX) :] if entry.startswith(TAMPER_PREFIX) else entry
