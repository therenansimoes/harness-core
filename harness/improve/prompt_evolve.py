"""Ação 'prompt': PromptBreeder-lite sobre prompts/**.

Operadores DETERMINÍSTICOS (rng seedado → mesmo resultado): aqui não entra
LLM, entra mutação estrutural de texto. Mesmo fail-closed do codegen: o
genoma barra alvo fora de `prompts/**` via `mutate.check` ANTES de qualquer
escrita. `propose` só calcula o texto novo; `apply` escreve atômico guardando
o anterior; `revert` restaura byte a byte. Quem julga KEEP/DISCARD é o A/B
do loop — nunca este módulo.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Callable

from harness.genome.genome import Genome
from harness.improve import mutate, root_dir
from harness.ledger import store

ACTION = "prompt"

# Banco fixo de diretivas: add tira daqui, drop só remove o que veio daqui.
DIRECTIVES: tuple[str, ...] = (
    "- Prefira editar arquivo existente a reescrever do zero.",
    "- Confirme paths e símbolos lendo o arquivo antes de editar.",
    "- Sem dependência nova sem pedido explícito.",
    "- Um problema por vez: termine e verifique antes do próximo.",
    "- Saída curta: reporte resultado, não narração.",
)

_SECTION_MARK = "## "


@dataclass(frozen=True)
class _Target:
    """Contrato duck-typed do `mutate.check`: só precisa de `target_file`."""

    target_file: str


@dataclass(frozen=True)
class PromptMutation:
    """O que será escrito e o que é preciso para desfazer, em texto puro."""

    mutation_id: str
    target: str
    operator: str
    before_text: str
    after_text: str
    ts: str


def _split_sections(text: str) -> tuple[str, list[str]]:
    """Preâmbulo (tudo antes do primeiro `## `) + lista de seções."""
    lines = text.splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if l.startswith(_SECTION_MARK)]
    if not idx:
        return text, []
    pre = "".join(lines[: idx[0]])
    bounds = idx + [len(lines)]
    sections = ["".join(lines[a:b]) for a, b in zip(bounds, bounds[1:])]
    return pre, sections


def _reorder_sections(text: str, rng: Random) -> str:
    pre, sections = _split_sections(text)
    if len(sections) < 2:
        return text
    rng.shuffle(sections)
    return pre + "".join(sections)


def _add_directive(text: str, rng: Random) -> str:
    candidates = [d for d in DIRECTIVES if d not in text]
    if not candidates:
        return text
    chosen = rng.choice(candidates)
    return text.rstrip("\n") + "\n" + chosen + "\n"


def _drop_directive(text: str, rng: Random) -> str:
    present = [d for d in DIRECTIVES if d in text]
    if not present:
        return text
    chosen = rng.choice(present)
    lines = [l for l in text.splitlines() if l != chosen]
    return "\n".join(lines) + "\n"


def _drop_shortest_section(text: str, rng: Random) -> str:
    """Encurta removendo a seção 'menos usada' — proxy determinístico: a menor."""
    pre, sections = _split_sections(text)
    if len(sections) < 2:
        return text
    shortest = min(sections, key=len)
    sections.remove(shortest)
    return pre + "".join(sections)


OPERATORS: dict[str, Callable[[str, Random], str]] = {
    "reorder_sections": _reorder_sections,
    "add_directive": _add_directive,
    "drop_directive": _drop_directive,
    "drop_shortest_section": _drop_shortest_section,
}


def propose_prompt_mutation(
    target: Path | str,
    operator: str,
    rng: Random,
    root: Path | str | None = None,
    genome: Genome | None = None,
) -> PromptMutation:
    """Genoma → operador → texto novo. Nada toca o disco aqui: recusa
    (genoma ou operador desconhecido) não deixa rastro nenhum."""
    base = root_dir(root)
    t = Path(target)
    rel = (t.relative_to(base) if t.is_absolute() else t).as_posix()

    violations = mutate.check(_Target(target_file=rel), root=base, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)

    if operator not in OPERATORS:
        raise KeyError(
            f"operador desconhecido: {operator!r} (disponíveis: {', '.join(sorted(OPERATORS))})"
        )

    before = (base / rel).read_text(encoding="utf-8")
    after = OPERATORS[operator](before, rng)
    ts = store.now_iso()
    mid = hashlib.sha256(f"{rel}\0{ts}\0{after}".encode("utf-8")).hexdigest()[:12]
    return PromptMutation(
        mutation_id=mid,
        target=rel,
        operator=operator,
        before_text=before,
        after_text=after,
        ts=ts,
    )


def apply_prompt_mutation(
    mutation: PromptMutation, root: Path | str | None = None
) -> PromptMutation:
    """Escreve o texto novo, atômico. O anterior já viaja na mutação."""
    _write(root_dir(root) / mutation.target, mutation.after_text)
    return mutation


def revert_prompt_mutation(
    mutation: PromptMutation, root: Path | str | None = None
) -> None:
    """Restaura o texto anterior byte a byte — o DISCARD do A/B chama aqui."""
    _write(root_dir(root) / mutation.target, mutation.before_text)


def _write(path: Path, text: str) -> None:
    """Atômico como `codegen._write`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(path))
    tmp = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if real.exists():
        os.chmod(tmp, os.stat(real).st_mode & 0o7777)
    os.replace(tmp, real)


def action():
    """A ação registrável — o integrador chama `register_action(action())`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_prompt_mutation, apply=apply_prompt_mutation)
