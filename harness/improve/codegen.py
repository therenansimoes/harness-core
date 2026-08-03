"""Ação 'codegen': mutação de CÓDIGO em plugins/**, estilo Darwin Gödel.

`plugins/**` é a única zona onde o loop reescreve módulos Python. O caminho é
o MESMO fail-closed do `mutate.check`: genoma barra ANTES de qualquer escrita,
e sintaxe inválida (`ast.parse`) recusa antes do disco também. Cada mutação
escreve atômico guardando o fonte anterior e appenda linhagem em
`$HARNESS_DATA_DIR/lineage.jsonl` (default `data/lineage.jsonl` sob o root). Quem julga é um exame INJETADO (na vida real,
benchmarks/sealed): KEEP mantém, DISCARD restaura byte a byte — o código
mutado nunca julga a si mesmo.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.genome.genome import Genome
from harness.improve import lineage, mutate, root_dir
from harness.ledger import store

ACTION = "codegen"
KEEP = "KEEP"
DISCARD = "DISCARD"


class CodegenError(Exception):
    """Fonte com sintaxe inválida — nada foi escrito."""


@dataclass(frozen=True)
class _Target:
    """Contrato duck-typed do `mutate.check`: só precisa de `target_file`."""

    target_file: str


@dataclass(frozen=True)
class CodeMutation:
    """O que foi escrito e o que é preciso para desfazer.

    `before_source=None` significa arquivo NOVO: o desfazer é apagar.
    """

    mutation_id: str
    parent_id: str | None
    target: str
    before_source: str | None
    after_source: str
    ts: str


def propose_code_mutation(
    target: Path | str,
    new_source: str,
    parent_id: str | None = None,
    root: Path | str | None = None,
    genome: Genome | None = None,
) -> CodeMutation:
    """Genoma → sintaxe → escrita atômica → linhagem, nessa ordem: recusa
    (genoma ou sintaxe) não deixa rastro nenhum no disco."""
    base = root_dir(root)
    t = Path(target)
    rel = (t.relative_to(base) if t.is_absolute() else t).as_posix()

    violations = mutate.check(_Target(target_file=rel), root=base, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)

    try:
        ast.parse(new_source)
    except SyntaxError as e:
        raise CodegenError(f"sintaxe inválida para {rel}: {e}") from e

    path = base / rel
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    ts = store.now_iso()
    # determinístico como mutation_id do mutate: retomada não duplica id
    mid = hashlib.sha256(f"{rel}\0{ts}\0{new_source}".encode("utf-8")).hexdigest()[:12]

    _write(path, new_source)
    _append_lineage(base, {"id": mid, "parent_id": parent_id, "target": rel, "ts": ts})
    return CodeMutation(
        mutation_id=mid,
        parent_id=parent_id,
        target=rel,
        before_source=before,
        after_source=new_source,
        ts=ts,
    )


def judge_code_mutation(
    mutation: CodeMutation,
    run_exam: Callable[[], bool],
    root: Path | str | None = None,
) -> str:
    """KEEP se o exame injetado passa; DISCARD restaura o fonte anterior
    byte a byte (ou apaga, se o arquivo era novo). Em ambos os casos appenda
    um evento de veredito na linhagem (linha com `verdict`, sem `target`) —
    mutação DISCARDed não fica sem marca na árvore."""
    base = root_dir(root)
    if run_exam():
        verdict = KEEP
    else:
        verdict = DISCARD
        path = base / mutation.target
        if mutation.before_source is None:
            path.unlink(missing_ok=True)
        else:
            _write(path, mutation.before_source)
    _append_lineage(
        base, {"id": mutation.mutation_id, "verdict": verdict, "ts": store.now_iso()}
    )
    return verdict


def _write(path: Path, text: str) -> None:
    """Atômico como `mutate._write`, mas o alvo pode ainda não existir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(path))
    tmp = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if real.exists():
        os.chmod(tmp, os.stat(real).st_mode & 0o7777)
    os.replace(tmp, real)


def _append_lineage(base: Path, line: dict) -> None:
    # `lineage.lineage_path()` é relativo (`data/lineage.jsonl`) quando
    # $HARNESS_DATA_DIR não está setado — aí vale sob `base`, como antes; setado
    # (absoluto) ele manda, e escrita e leitura caem no MESMO arquivo.
    p = base / lineage.lineage_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def action():
    """A ação registrável — o integrador chama `register_action(action())`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_code_mutation, apply=judge_code_mutation)
