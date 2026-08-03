"""Ação 'node': o loop propõe um NÓ novo para o grafo — ack humano obrigatório.

Mesmo caminho do `codegen` (é uma mutação de código em `plugins/**`, escrita
atômica + linhagem + exame selado como juiz), com um degrau a mais: o `meta`
guarda a régua e o governor porque mudá-los é mexer em quem vigia o loop; um nó
novo é o loop mexendo no PRÓPRIO grafo, código arbitrário rodando dentro da
execução. Então aqui o `human_ack` não é condicional ao alvo: é sempre exigido,
via `$HARNESS_NODE_ACK=1`, que nenhum caminho do autopilot produz.

Sem ack (ou com exame reprovado) a mutação é DESCARTADA pelo próprio
`judge_code_mutation` — exame falso, arquivo removido, veredito na linhagem — e
nada é aprovado. Com ack, o sha256 do arquivo escrito vai para
`node_approvals.jsonl`, que é o que `plugin_nodes.register_all` exige depois.

Ainda NÃO registrada em `target.actions()`: o wiring é o passo seguinte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.genome.genome import Genome
from harness.graph import plugin_nodes
from harness.improve import codegen, exam, meta

ACTION = "node"

# Ack humano: `1` exato. "true"/"yes" fora — ack é ato deliberado, não parse
# generoso de env que alguém deixou setada.
ACK_ENV = "HARNESS_NODE_ACK"
ACK_ON = "1"


class NodeActionError(Exception):
    """Proposta malformada (nome) — nada foi escrito."""


@dataclass(frozen=True)
class NodeProposal:
    """`target_file` é o que o genome_check julga — mesmo contrato duck-typed
    do `mutate.check`."""

    name: str
    source: str
    target_file: str


@dataclass(frozen=True)
class NodeRecord:
    """O veredito completo, auditável: quem julgou o quê e se houve ack."""

    name: str
    target_file: str
    verdict: str
    meta_verdict: str
    exam_passed: bool
    human_ack: bool
    approved: bool
    sha256: str | None


def target_file(name: str) -> str:
    return f"{plugin_nodes.NODES_SUBDIR.as_posix()}/{name}.py"


def render_node_source(name: str, note: str = "") -> str:
    """Template FIXO: pass-through que só emite o próprio evento.

    O template não é backend, é esqueleto — o valor da ação é o caminho de
    aprovação, não a criatividade do corpo. Nada aqui viola o guard de AST do
    `plugin_nodes` (sem subprocess/socket/ctypes/multiprocessing, sem eval);
    quem evoluir o corpo depois passa pelo mesmo gate, com hash novo.
    """
    lines = [
        f"# Nó de plugin {name!r} proposto pela ação '{ACTION}'.",
        "# Só entra no grafo com sha256 aprovado por humano (node_approvals.jsonl).",
    ]
    if note:
        lines.append(f"# Nota: {note.splitlines()[0]}")
    lines += [
        "",
        "from harness.ledger import store",
        "",
        f"NAME = {name!r}",
        "",
        "",
        "def node(state, config=None) -> dict:",
        '    """Pass-through: registra passagem e não escreve mais nada no estado."""',
        '    return {"events": [{"node": NAME, "at": store.now_iso()}]}',
    ]
    return "\n".join(lines) + "\n"


def propose_node(
    name: str,
    note: str = "",
    root: Path | str | None = None,
    genome: Genome | None = None,
) -> NodeProposal:
    """Valida o nome ANTES de qualquer escrita: nome que o registry recusaria
    depois seria arquivo escrito para nunca carregar."""
    from harness.graph import topology

    if not plugin_nodes.NAME_RE.fullmatch(name):
        raise NodeActionError(
            f"nome de nó inválido: {name!r} (esperado {plugin_nodes.NAME_RE.pattern})"
        )
    if name in topology.NODE_IMPLS:
        raise NodeActionError(f"nome de nó já existe na whitelist: {name!r}")
    return NodeProposal(
        name=name,
        source=render_node_source(name, note),
        target_file=target_file(name),
    )


def apply_node(
    proposal: NodeProposal,
    root: Path | str | None = None,
    genome: Genome | None = None,
    run_exam: Callable[[], bool] | None = None,
    human_ack: bool | None = None,
    data_dir: Path | str | None = None,
) -> NodeRecord:
    """Escreve (via codegen, genoma fail-closed) → exame → meta → ack → aprova.

    O exame roda antes do ack porque ack humano em código reprovado é ack
    inútil; e o `meta_check` entra mesmo devolvendo "allowed" para este alvo:
    é ele que decide o que é guardado, não esta ação.
    """
    mutation = codegen.propose_code_mutation(
        proposal.target_file, proposal.source, root=root, genome=genome
    )
    exam_fn = run_exam if run_exam is not None else exam.run_sealed_exam
    passed = bool(exam_fn())
    ack = _ack() if human_ack is None else bool(human_ack)
    meta_verdict = meta.meta_check(
        Path(proposal.target_file), run_sealed_exam=lambda: passed, human_ack=ack
    )

    keep = passed and ack and meta_verdict == meta.ALLOWED
    verdict = codegen.judge_code_mutation(
        mutation, run_exam=lambda: keep, root=root
    )
    digest: str | None = None
    if verdict == codegen.KEEP:
        from harness.improve import root_dir

        digest = plugin_nodes.file_sha256(root_dir(root) / proposal.target_file)
        plugin_nodes.record_approval(proposal.name, digest, data_dir)
    return NodeRecord(
        name=proposal.name,
        target_file=proposal.target_file,
        verdict=verdict,
        meta_verdict=meta_verdict,
        exam_passed=passed,
        human_ack=ack,
        approved=digest is not None,
        sha256=digest,
    )


def _ack() -> bool:
    return os.environ.get(ACK_ENV, "").strip() == ACK_ON


def action():
    """A ação registrável — o integrador chama `register_action(action())`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_node, apply=apply_node)
