"""Routing: `kind` (o QUE a unidade é) é ortogonal a `tier` (QUANTO custa)."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR_ENV = "HARNESS_CONFIG_DIR"


def config_dir() -> Path:
    """Onde vivem os TOML calibráveis. O env var existe para o teste apontar
    para um tmpdir — mesma convenção do `$HARNESS_DATA_DIR` do ledger."""
    return Path(os.environ.get(CONFIG_DIR_ENV, "config"))
