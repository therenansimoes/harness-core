"""Adaptadores de ação: synthesize, topology e evolve viram `Action`.

Os módulos por trás (improve/synthesize, graph/topology, evolve/population)
têm a infra mas não expõem o par propose/apply que o registry de
`target.actions()` consome. Este módulo é SÓ o adaptador — nenhuma lógica nova
de síntese/validação/mutação mora aqui.

Contrato comum, no padrão de research/codegen:
- `propose_*` devolve proposta ou None (None = sem gradiente, não "faça algo").
- `apply_*` checa o genoma fail-closed ANTES de escrever (`mutate.check` com o
  duck-typing de `target_file`); violação levanta `GenomeViolation` e nada
  toca o disco. Topologia inválida recusa via `topology.compile_spec` antes
  da escrita.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.improve import mutate, root_dir
from harness.improve import synthesize as synth
from harness.types import RunRow

SYNTHESIZE = "synthesize"
TOPOLOGY = "topology"
EVOLVE = "evolve"

TOPOLOGY_FILE = "config/topology.toml"
EVOLVE_DEFAULT_FILE = "config/models.toml"

# Nó da whitelist que a variação de topologia insere: pass-through barato,
# a única mudança estrutural que dá pra propor sem tocar nos nós.
REFLECT = "reflect"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ActionError(Exception):
    """Proposta inaplicável — nada foi escrito."""


def _write_file(path: Path, text: str) -> None:
    """Atômico como `mutate._write`, mas o alvo pode ainda não existir
    (mesmo racional do `codegen._write`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(path))
    tmp = real.with_name(f".{real.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if real.exists():
        os.chmod(tmp, os.stat(real).st_mode & 0o7777)
    os.replace(tmp, real)


def _genome_gate(proposal: Any, root: Path | str | None, genome: Any) -> None:
    violations = mutate.check(proposal, root=root, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)


# --- synthesize -----------------------------------------------------------------


@dataclass(frozen=True)
class SynthesizeProposal:
    """Falhas candidatas a virarem exame de quarentena. `rows` são as linhas
    do ledger que a síntese consome; `unit_ids` é o resumo auditável."""

    unit_ids: tuple[str, ...]
    rows: tuple[RunRow, ...]


def propose_synthesize(
    history: Sequence[RunRow] = (),
) -> SynthesizeProposal | None:
    """Lista as falhas do ledger dedupadas por unidade. Sem falha => None."""
    seen: set[str] = set()
    rows: list[RunRow] = []
    for row in history:
        failed = (not row.ok) or row.exit_reason == synth.REVERTED
        if not failed or row.unit_id in seen:
            continue
        seen.add(row.unit_id)
        rows.append(row)
    if not rows:
        return None
    return SynthesizeProposal(
        unit_ids=tuple(r.unit_id for r in rows), rows=tuple(rows)
    )


def apply_synthesize(
    proposal: SynthesizeProposal, root: Path | str | None = None
) -> list[Path]:
    """Delegação direta: quem escreve (e NUNCA em sealed) é o synthesize."""
    base = root_dir(root)
    return synth.synthesize_from_failures(
        proposal.rows,
        out_dir=base / synth.QUARANTINE_DIR,
        units_dir=base / synth.DEFAULT_UNITS_DIR,
    )


def synthesize_action():
    from harness.improve.target import Action

    return Action(name=SYNTHESIZE, propose=propose_synthesize, apply=apply_synthesize)


# --- topology -------------------------------------------------------------------


@dataclass(frozen=True)
class TopologyProposal:
    """Nova spec inteira como texto: a validação/aplicação relê o TOML — o que
    o `apply` julga é exatamente o que iria pro disco."""

    target_file: str
    new_text: str
    reasons: tuple[str, ...] = ()


def render_topology(spec: Mapping[str, Any]) -> str:
    nodes = "".join(f"  {json.dumps(n)},\n" for n in spec["nodes"])
    edges = "".join(
        f"  [{json.dumps(a)}, {json.dumps(b)}],\n" for a, b in spec["edges"]
    )
    return f"nodes = [\n{nodes}]\n\nedges = [\n{edges}]\n"


def propose_topology(
    root: Path | str | None = None, spec_path: Path | str | None = None
) -> TopologyProposal | None:
    """Variação VÁLIDA: insere `reflect` numa aresta linear. `reflect` já
    presente => None (a única variação que sabemos propor já foi feita)."""
    from harness.graph import topology

    base = root_dir(root)
    p = Path(spec_path) if spec_path is not None else base / TOPOLOGY_FILE
    spec = tomllib.loads(p.read_text(encoding="utf-8"))
    nodes = list(spec.get("nodes", ()))
    edges = [tuple(e) for e in spec.get("edges", ())]
    if REFLECT in nodes:
        return None

    # Splice determinístico: a primeira aresta linear que não sai do gate nem
    # dos terminais — dois ciclos com a mesma spec propõem a mesma variação.
    idx = next(
        (
            i
            for i, (src, dst) in enumerate(edges)
            if src not in ("gate", topology.START_NAME)
            and dst != topology.END_NAME
        ),
        None,
    )
    if idx is None:
        return None
    src, dst = edges[idx]
    new_edges = edges[:idx] + [(src, REFLECT), (REFLECT, dst)] + edges[idx + 1 :]
    new_spec = {"nodes": nodes + [REFLECT], "edges": [list(e) for e in new_edges]}
    topology._validate(new_spec)  # proposta já nasce válida ou não nasce
    return TopologyProposal(
        target_file=TOPOLOGY_FILE,
        new_text=render_topology(new_spec),
        reasons=(f"insert:{REFLECT}@{src}->{dst}",),
    )


def apply_topology(
    proposal: TopologyProposal,
    root: Path | str | None = None,
    genome: Any = None,
) -> Path:
    """Genoma -> compile -> escrita atômica, nessa ordem. Spec que não compila
    recusa (`TopologyError`/`TOMLDecodeError`) sem tocar o disco."""
    from harness.graph import topology

    _genome_gate(proposal, root, genome)
    spec = tomllib.loads(proposal.new_text)
    topology.compile_spec(spec)  # inválida => sobe, fail-closed
    path = root_dir(root) / proposal.target_file
    _write_file(path, proposal.new_text)
    return path


def topology_action():
    from harness.improve.target import Action

    return Action(name=TOPOLOGY, propose=propose_topology, apply=apply_topology)


# --- evolve ---------------------------------------------------------------------


@dataclass(frozen=True)
class EvolveProposal:
    """Config candidata inteira. `seed` fica no registro: a proposta tem que
    ser reproduzível a partir do state que a gerou."""

    target_file: str
    candidate: dict
    seed: int


def evolve_seed(state: Mapping[str, Any] | None) -> int:
    """Seed determinística do state (thread + ciclo): retomada re-propõe o
    MESMO candidato, não um novo sorteio."""
    thread = str((state or {}).get("thread_id", ""))
    cycle = int((state or {}).get("cycle", 0))
    digest = hashlib.sha256(f"{thread}\0{cycle}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def propose_evolve(
    state: Mapping[str, Any] | None = None,
    target_file: str = EVOLVE_DEFAULT_FILE,
    root: Path | str | None = None,
) -> EvolveProposal:
    """Candidato = `population.mutate_config` sobre o config alvo, rng seedado
    do state — mesmo state, mesma proposta."""
    from harness.evolve import population

    base = root_dir(root)
    current = tomllib.loads((base / target_file).read_text(encoding="utf-8"))
    seed = evolve_seed(state)
    candidate = population.mutate_config(current, random.Random(seed))
    return EvolveProposal(target_file=target_file, candidate=candidate, seed=seed)


def _toml_key(key: Any) -> str:
    """Chave bare quando dá; senão basic string (json.dumps é TOML válido)."""
    k = str(key)
    return k if _BARE_KEY.match(k) else json.dumps(k)


def _dump_toml(data: Mapping[str, Any], prefix: str = "") -> str:
    """Dict -> TOML. Só o que `mutate_config` produz: escalares, listas,
    tabelas e arrays de tabelas."""
    scalars: list[str] = []
    tables: list[str] = []
    for key, value in data.items():
        k = _toml_key(key)
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(value, Mapping):
            tables.append(f"[{full}]\n{_dump_toml(value, prefix=full)}")
        elif isinstance(value, list) and value and all(
            isinstance(v, Mapping) for v in value
        ):
            tables.extend(
                f"[[{full}]]\n{_dump_toml(v, prefix=full)}" for v in value
            )
        elif isinstance(value, list):
            items = ", ".join(mutate._render(v) for v in value)
            scalars.append(f"{k} = [{items}]")
        else:
            scalars.append(f"{k} = {mutate._render(value)}")
    body = "\n".join(scalars)
    parts = ([body] if body else []) + tables
    return "\n\n".join(parts) + ("\n" if parts else "")


def apply_evolve(
    proposal: EvolveProposal,
    root: Path | str | None = None,
    genome: Any = None,
) -> Path:
    """Genoma -> serializa -> roundtrip -> escrita atômica. O roundtrip é o
    cinto e suspensório do research: candidato que não sobrevive ao parser
    oficial não chega ao disco."""
    _genome_gate(proposal, root, genome)
    text = _dump_toml(proposal.candidate)
    if tomllib.loads(text) != proposal.candidate:
        raise ActionError(f"candidato não sobrevive ao roundtrip TOML: {proposal.target_file}")
    path = root_dir(root) / proposal.target_file
    _write_file(path, text)
    return path


def evolve_action():
    from harness.improve.target import Action

    return Action(name=EVOLVE, propose=propose_evolve, apply=apply_evolve)


def builtin_actions():
    """As três ações deste módulo — consumidas por `target.actions()`."""
    return (synthesize_action(), topology_action(), evolve_action())
