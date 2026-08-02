"""Routing: `kind` (o QUE a unidade é) é ortogonal a `tier` (QUANTO custa)."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"

# Quem escolhe o executor: o chamador (`manual`) ou o router (`auto`). Mora
# aqui, e não no grafo, porque a CLI usa o mesmo vocabulário sem importar
# langgraph.
ROUTE_MANUAL = "manual"
ROUTE_AUTO = "auto"
ROUTE_MODES = (ROUTE_MANUAL, ROUTE_AUTO)

# Tier das seleções manuais. Nome fora da tabela de custo de propósito: o prior
# é keyed em (kind, tier, backend), então run escolhido no dedo não vira
# evidência a favor (nem contra) de nenhum tier de verdade.
MANUAL_TIER = "manual"


def config_dir() -> Path:
    """Onde vivem os TOML calibráveis. O env var existe para o teste apontar
    para um tmpdir — mesma convenção do `$HARNESS_DATA_DIR` do ledger."""
    return Path(os.environ.get(CONFIG_DIR_ENV, "config"))
