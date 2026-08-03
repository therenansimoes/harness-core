"""Prova selada: o verificador não fica visível para o agente.

Se o agente lê o `verify.py` da unidade, ele recomputa o golden em vez de
resolver a tarefa — a régua deixa de medir a capacidade e passa a medir leitura
de arquivo. Por isso o verificador sai do seed do workspace e só é materializado
no instante do verify, sendo removido depois: a tentativa seguinte do retry
também roda às cegas.

Convenção de nome, não campo novo no `unit.toml`: quem escreve unidade nova não
precisa lembrar de marcar nada. `fixtures/` fica de fora da retenção — nas
unidades seladas atuais a fixture é insumo do agente (o prompt manda copiá-la
para o diretório de trabalho), e o verify_cmd a copia de novo antes de medir.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Iterator

VERIFIER_NAMES: tuple[str, ...] = ("verify.py",)


def is_verifier(rel: Path | str) -> bool:
    """Casa por nome de arquivo em qualquer profundidade (`verify.py`, `sub/verify.py`)."""
    return Path(rel).name in VERIFIER_NAMES


@contextlib.contextmanager
def verifier_visible(unit_path: Path, ws: Path) -> Iterator[list[str]]:
    """Materializa os verificadores da unidade no workspace e desfaz na saída.

    Só copia o que ainda não existe no workspace e só remove o que copiou: em
    `--repo` o verificador pode ser arquivo legítimo do alvo, e apagá-lo seria
    destruir trabalho de quem chamou.
    """
    copied: list[Path] = []
    if unit_path.is_dir():
        for src in sorted(unit_path.rglob("*")):
            rel = src.relative_to(unit_path)
            if not src.is_file() or not is_verifier(rel):
                continue
            dst = ws / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(dst)
    try:
        yield [p.relative_to(ws).as_posix() for p in copied]
    finally:
        for dst in copied:
            dst.unlink(missing_ok=True)
