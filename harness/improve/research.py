"""Ação 'research': transforma falha repetida em skill destilada.

A mutação de config calibra um knob; research ataca a outra metade do
gradiente — o kind que falha repetido não por knob errado, mas por falta de
conhecimento. A ação executa um prompt de pesquisa num backend registrado e
grava o destilado como `skills/<slug>.md` no formato de skill compartilhado
(frontmatter TOML entre dois `---`, corpo em markdown).

O caminho é o MESMO das outras mutações: `mutate.check` fail-closed ANTES de
gastar backend e antes de escrever — `skills/**` só é gravável se o genoma o
declarar mutável. Violação levanta `GenomeViolation`, não escreve nada.

A escrita é de arquivo NOVO, não cirurgia de token: skill não existe antes da
pesquisa, então não há `before_raw` para devolver — o revert de uma skill é
apagar o arquivo, e quem decide isso é o ciclo, não esta função.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from harness.backends.registry import get_backend
from harness.genome.genome import Genome
from harness.improve import mutate, root_dir
from harness.improve.target import failure_pattern
from harness.ledger import store
from harness.types import ExecRequest, RunRow

ACTION = "research"
SKILLS_SUBDIR = "skills"

# Falha "repetida" = pelo menos isto no mesmo kind. Maior que o min_fail_n=1
# do pick_target de propósito: pesquisa custa LLM, uma falha isolada não paga.
MIN_FAIL_N = 2

_SLUG = re.compile(r"[^a-z0-9]+")


class ResearchError(Exception):
    """Backend falhou ou devolveu pesquisa vazia — nada foi escrito."""


@dataclass(frozen=True)
class ResearchProposal:
    """O que pesquisar e onde a skill vai parar. `target_file` é o que o
    genome_check julga — mesmo contrato duck-typed do `mutate.check`."""

    topic: str
    kind: str
    slug: str
    target_file: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchRecord:
    """O que foi escrito. Só `str`, pelo motivo da `Mutation`: atravessa o
    checkpoint como dict inerte sem perder informação."""

    topic: str
    kind: str
    slug: str
    skill_path: str
    backend: str
    written_at: str


def slugify(topic: str) -> str:
    slug = _SLUG.sub("-", topic.lower()).strip("-")
    if not slug:
        raise ResearchError(f"tópico sem slug possível: {topic!r}")
    return slug


def _proposal(topic: str, kind: str, reasons: tuple[str, ...]) -> ResearchProposal:
    slug = slugify(topic)
    return ResearchProposal(
        topic=topic,
        kind=kind,
        slug=slug,
        target_file=f"{SKILLS_SUBDIR}/{slug}.md",
        reasons=reasons,
    )


def propose_research(
    history: Sequence[RunRow] = (),
    topic: str | None = None,
    kind: str | None = None,
    min_fail_n: int = MIN_FAIL_N,
) -> ResearchProposal | None:
    """Alvo de pesquisa: tópico explícito, ou o kind mais falho do ledger.

    Sem tópico e sem kind com falha repetida devolve None — mesmo contrato do
    `pick_target`: None não é "pesquise qualquer coisa", é sem gradiente.
    Empate desempata por nome do kind, pelo motivo de sempre: dois ciclos com
    a mesma evidência têm que propor a mesma pesquisa.
    """
    if topic is not None:
        return _proposal(topic, kind or "code", ("topic:explicit",))

    fails: dict[str, list[str]] = {}
    for row in history:
        if row.ok or row.kind is None:
            continue
        fails.setdefault(row.kind, []).append(failure_pattern(row.exit_reason))

    scored = [(len(v), k) for k, v in fails.items() if len(v) >= min_fail_n]
    if not scored:
        return None
    n, chosen = min(scored, key=lambda s: (-s[0], s[1]))
    patterns = fails[chosen]
    pattern = min(set(patterns), key=lambda p: (-patterns.count(p), p))
    return _proposal(
        f"falhas {pattern} em unidades {chosen}",
        chosen,
        (f"kind:{chosen}({n} falhas)", f"pattern:{pattern}"),
    )


def research_prompt(proposal: ResearchProposal) -> str:
    """Pede destilado acionável, não redação: a skill é lida por outro agente
    no meio de um run, cada linha tem que pagar o próprio contexto."""
    ctx = f" Evidência: {', '.join(proposal.reasons)}." if proposal.reasons else ""
    return (
        f"Pesquise e DESTILE orientação acionável sobre: {proposal.topic}."
        f" Contexto: unidades kind={proposal.kind} falhando repetidamente.{ctx}\n"
        "Responda só com a orientação destilada, em markdown: causas prováveis,"
        " passos concretos de correção e como verificar. Sem preâmbulo."
    )


def apply_research(
    proposal: ResearchProposal,
    backend: str = "mock",
    root: Path | str | None = None,
    genome: Genome | None = None,
    model: str | None = None,
    timeout_s: float = 600.0,
) -> ResearchRecord:
    """Checa o genoma, executa a pesquisa e grava a skill. Nessa ordem.

    O genoma vem ANTES do backend: pesquisa barrada não pode nem gastar LLM —
    é o mesmo fail-closed do `mutate.apply`, com a mesma exceção, para que o
    chamador trate REJECTED por um caminho só.
    """
    violations = mutate.check(proposal, root=root, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)

    base = root_dir(root)
    workspace = base / "data" / "research" / proposal.slug
    result = get_backend(backend).execute(
        ExecRequest(
            prompt=research_prompt(proposal),
            workspace=workspace,
            model=model,
            timeout_s=timeout_s,
            trace_path=workspace / "trace.jsonl",
        )
    )
    if not result.ok:
        raise ResearchError(f"backend {backend!r} falhou: {result.exit_reason}")

    body = _distill(workspace, result.files_changed)
    ts = store.now_iso()
    text = render_skill(proposal, body)
    _parse_skill(text)  # cinto e suspensório: skill ilegível não chega ao disco
    _write_new(base / proposal.target_file, text)
    return ResearchRecord(
        topic=proposal.topic,
        kind=proposal.kind,
        slug=proposal.slug,
        skill_path=proposal.target_file,
        backend=backend,
        written_at=ts,
    )


def render_skill(proposal: ResearchProposal, body: str) -> str:
    """Formato compartilhado de skill: `---`, TOML, `---`, corpo markdown."""
    # `mutate._render` é privado, mas escapar aspas por conta própria seria a
    # segunda rotina de string->TOML do pacote — mesmo argumento do `_relative`.
    name = mutate._render(proposal.slug)
    kinds = mutate._render(proposal.kind)
    desc = mutate._render(f"orientação destilada: {proposal.topic}".splitlines()[0])
    return (
        "---\n"
        f"name = {name}\n"
        f"kinds = [{kinds}]\n"
        f"description = {desc}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )


def _distill(workspace: Path, files_changed: Sequence[str]) -> str:
    """Corpo da skill = o que o backend escreveu no workspace. Vazio é erro:
    skill sem corpo é arquivo que engana o próximo run."""
    parts: list[str] = []
    for name in files_changed:
        p = workspace / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8").strip())
    body = "\n\n".join(part for part in parts if part)
    if not body:
        raise ResearchError(f"pesquisa vazia: backend não deixou texto em {workspace}")
    return body


def _parse_skill(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ResearchError("skill sem frontmatter delimitado por '---'")
    end = lines[1:].index("---") + 1
    return tomllib.loads("\n".join(lines[1:end]))


def _write_new(path: Path, text: str) -> None:
    """Atômico como `mutate._write`, mas para arquivo NOVO: `_write` copia o
    modo de um arquivo existente, aqui o modo do umask é o certo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def action():
    """A ação registrável — consultada por `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_research, apply=apply_research)
