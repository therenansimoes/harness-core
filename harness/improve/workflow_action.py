"""Ação 'workflow': o loop propõe e grava workflows nomeados.

Mesmo padrão de research/codegen: `propose_*` valida TUDO antes (compile da
spec via topology — inválida recusa sem escrever nada), `apply_*` passa pelo
`mutate.check` fail-closed (duck-typed em `target_file`) e só então escreve
atômico em `config/workflows/<name>.toml`. O genoma decide se a zona é
mutável; violação levanta `GenomeViolation` e nada toca o disco.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harness.graph import topology
from harness.improve import mutate, root_dir
from harness.ledger import store

ACTION = "workflow"
WORKFLOWS_SUBDIR = "config/workflows"

# Nome vira arquivo: slug estrito barra path traversal e nome vazio.
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


class WorkflowActionError(Exception):
    """Proposta malformada (nome/entrada) — nada foi escrito."""


@dataclass(frozen=True)
class WorkflowProposal:
    """`target_file` é o que o genome_check julga — mesmo contrato
    duck-typed do `mutate.check`."""

    name: str
    toml_text: str
    target_file: str


@dataclass(frozen=True)
class WorkflowRecord:
    """O que foi escrito. Só `str`: atravessa checkpoint como dict inerte."""

    name: str
    workflow_path: str
    written_at: str


def _render_toml(spec: Mapping[str, Any]) -> str:
    # json.dumps de listas de strings é TOML válido; os nomes vêm da
    # whitelist de nós, então não há escape exótico a temer.
    nodes = json.dumps(list(spec.get("nodes", [])))
    edges = json.dumps([list(e) for e in spec.get("edges", [])])
    return (
        "# Workflow proposto pelo loop — mutável, validado por compile + genoma.\n"
        f"nodes = {nodes}\n"
        f"edges = {edges}\n"
    )


def propose_workflow(
    name: str,
    spec: Mapping[str, Any] | None = None,
    toml_text: str | None = None,
) -> WorkflowProposal:
    """Valida compile ANTES de qualquer escrita: spec inválida => o mesmo
    TopologyError do runtime, e a proposta nem nasce."""
    if not _NAME.fullmatch(name):
        raise WorkflowActionError(f"nome de workflow inválido: {name!r}")
    if (spec is None) == (toml_text is None):
        raise WorkflowActionError("passe exatamente um: spec ou toml_text")
    if toml_text is None:
        toml_text = _render_toml(spec)
    try:
        parsed = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as e:
        raise topology.TopologyError(f"workflow {name!r}: toml torto: {e}") from e
    topology.compile_spec(parsed)  # inválida => TopologyError, sem escrita
    return WorkflowProposal(
        name=name,
        toml_text=toml_text,
        target_file=f"{WORKFLOWS_SUBDIR}/{name}.toml",
    )


def apply_workflow(
    proposal: WorkflowProposal,
    root: Path | str | None = None,
    genome=None,
) -> WorkflowRecord:
    """Genoma ANTES da escrita — mesmo fail-closed do `mutate.apply`, mesma
    exceção, para o chamador tratar REJECTED por um caminho só."""
    violations = mutate.check(proposal, root=root, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)
    # Revalida: a proposta pode ter atravessado um checkpoint desde o propose.
    topology.compile_spec(tomllib.loads(proposal.toml_text))

    path = root_dir(root) / proposal.target_file
    _write_new(path, proposal.toml_text)
    return WorkflowRecord(
        name=proposal.name,
        workflow_path=proposal.target_file,
        written_at=store.now_iso(),
    )


def _write_new(path: Path, text: str) -> None:
    """Atômico (tmp irmão + os.replace): config truncada é o pior estado."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def action():
    """A ação registrável — consultada por `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_workflow, apply=apply_workflow)
