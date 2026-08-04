"""Tamper: prova que a régua não mudou embaixo da própria run.

Duas perguntas, ambas fechadas. `check_patch` responde "o patch tocou o que o
genoma proíbe?" — e depende da lista de arquivos que alguém declarou ter
mudado. `fingerprint` responde "o conteúdo do imutável é o mesmo do início da
run?" — e não depende de ninguém declarar nada. A segunda existe porque a
primeira, sozinha, confia no declarante.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from harness.genome.genome import (
    DEFAULT_PATH,
    Genome,
    check_patch,
    load,
    matches,
    search_root,
    violation_path,
)

GENOME_VIOLATION = "tamper:genome_violation"
IMMUTABLE_CHANGED = "tamper:immutable_changed"

SKIP_DIRS = frozenset({".git", "__pycache__"})


def immutable_files(g: Genome, root: Path) -> list[str]:
    """Os arquivos que existem hoje sob a blocklist, ordenados.

    Varre a partir do prefixo literal de cada padrão: começar na raiz
    arrastaria `.git`, `node_modules` e workspaces de run junto.
    """
    base = Path(root)
    out: set[str] = set()
    for pat in g.immutable:
        head = search_root(pat)
        start = base / head if head else base
        if start.is_file():
            candidates = [start]
        elif start.is_dir():
            candidates = [p for p in start.rglob("*") if p.is_file()]
        else:
            candidates = []
        for p in candidates:
            if SKIP_DIRS & set(p.parts):
                continue
            rel = p.relative_to(base).as_posix()
            if matches(rel, g.immutable):
                out.add(rel)
    return sorted(out)


def fingerprint(g: Genome, root: Path) -> str:
    """SHA-256 dos imutáveis: cada path e o hash do seu conteúdo, em ordem.

    Estável enquanto nada muda; muda se um byte mudar, ou se um arquivo
    imutável sumir ou aparecer.
    """
    base = Path(root)
    h = hashlib.sha256()
    for rel in immutable_files(g, base):
        content = hashlib.sha256((base / rel).read_bytes()).hexdigest()
        h.update(f"{rel}\0{content}\n".encode())
    return h.hexdigest()


def detect(
    root: Path,
    before: str,
    changed: Iterable[str],
    genome: Genome | None = None,
) -> list[str]:
    """Violações do patch + prova de que o imutável não mudou desde `before`.

    `genome` default é o `config/genome.toml` de `root`. Quem checa uma
    sandbox passa o genoma canônico do repo — a cópia lá dentro é justamente
    o que está sob suspeita.
    """
    base = Path(root)
    g = genome if genome is not None else load(base / DEFAULT_PATH)
    out = [f"{GENOME_VIOLATION}:{violation_path(v)}" for v in check_patch(g, changed, root=base)]
    if fingerprint(g, base) != before:
        out.append(IMMUTABLE_CHANGED)
    return out
