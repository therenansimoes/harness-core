"""Loop de melhoria: escolhe uma mutação de config, testa em A/B, decide.

Tudo aqui é relativo a uma RAIZ, não ao cwd: `catalog.toml`, `genome.toml` e o
arquivo que a regra muta precisam vir da mesma árvore, senão o loop calibraria
um `config/` e mediria outro. `$HARNESS_ROOT` existe pelo mesmo motivo que
`$HARNESS_DATA_DIR` e `$HARNESS_CONFIG_DIR`: deixar o teste rodar num tmpdir.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT_ENV = "HARNESS_ROOT"
CONFIG_SUBDIR = "config"
CATALOG_FILE = "catalog.toml"
GENOME_FILE = "genome.toml"


def root_dir(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else Path(os.environ.get(ROOT_ENV, "."))


def catalog_path(root: Path | str | None = None) -> Path:
    return root_dir(root) / CONFIG_SUBDIR / CATALOG_FILE


def genome_path(root: Path | str | None = None) -> Path:
    return root_dir(root) / CONFIG_SUBDIR / GENOME_FILE
