"""Meta-exame: quem julga mudança nos parâmetros do próprio juiz.

`config/ruler.toml` é mutável, mas é a régua do gate — mudá-lo exige exame
mais duro que uma mutação comum: o exame selado tem que passar E um humano
tem que dar ack. Sem ack, a mudança fica em quarentena (não aplica).

`human_ack=True` só pode vir de CLI operada por humano. O autopilot NUNCA
passa True aqui — não existe caminho de código no loop que o produza; o
default do loop é False e ponto. Quebrar isso é tamper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal

MetaVerdict = Literal["allowed", "quarantined", "blocked"]
ALLOWED, QUARANTINED, BLOCKED = "allowed", "quarantined", "blocked"

RULER_CONFIG_NAME = "ruler.toml"
GOVERNOR_CONFIG_NAME = "governor.toml"
RULER_CONFIG_DIR = "config"
# O guarda cobre o juiz E o chefe: mudar a régua do gate ou o prazo/pressão do
# governor é o loop mexendo em quem o vigia — mesmo exame duro para os dois.
GUARDED_CONFIG_NAMES = frozenset({RULER_CONFIG_NAME, GOVERNOR_CONFIG_NAME})


def _targets_ruler_config(target: Path) -> bool:
    """Casa por sufixo `config/{ruler,governor}.toml` — path relativo ou absoluto."""
    t = Path(target)
    return t.name in GUARDED_CONFIG_NAMES and t.parent.name == RULER_CONFIG_DIR


def meta_check(
    mutation_target: Path,
    run_sealed_exam: Callable[[], bool],
    human_ack: bool,
) -> MetaVerdict:
    """Veredito sobre uma mutação, em ordem:

    1. alvo fora de config/{ruler,governor}.toml -> "allowed" (exame nem roda)
    2. exame selado falhou                  -> "blocked"
    3. exame ok, sem ack humano             -> "quarantined" (não aplica)
    4. exame ok + ack humano                -> "allowed"

    "quarantined" não é rejeição: a proposta sobrevive esperando o ack.
    """
    if not _targets_ruler_config(mutation_target):
        return ALLOWED
    if not run_sealed_exam():
        return BLOCKED
    if not human_ack:
        return QUARANTINED
    return ALLOWED
