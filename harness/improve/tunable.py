"""O que o `tune` sabe afinar, e o que cada tipo de artefato tem de próprio.

O loop de tuning é UM só: congela o exame, produz a saída de cada caso, pontua,
compara, retém. O que muda entre afinar uma skill e afinar um workflow é só
quatro coisas — de onde o texto vem, o que torna esse texto válido, como se
produz a saída de um caso e onde ele volta a ser gravado. Isso é o `Tunable`.

A separação importa por um motivo concreto: a saída de um caso de SKILL só
existe passando o texto por um modelo (é isso que uma skill faz), enquanto a de
um WORKFLOW é uma função pura da spec compilada. Se o protocolo não separasse
os dois, ou o workflow pagaria LLM para ser medido, ou a skill seria medida por
um render que não é o que ela faz na vida real.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from harness.evals.bundle import EvalCase
from harness.evals.score import Aggregate, TrialResult
from harness.graph import topology
from harness.improve import research, root_dir, workflow_action

WORKFLOWS_PREFIX = "config/workflows/"
SKILLS_PREFIX = "skills/"

# `render_skill` sempre prefixa a descrição com isto; tirar antes de re-render
# é o que torna a reescrita idempotente (senão a descrição cresce por versão).
_DESC_PREFIX = "orientação destilada: "


class Tunable(Protocol):
    """O contrato do artefato afinável. `validate` devolve LISTA (vazia = ok),
    nunca levanta: versão inválida é dado do relatório, não crash do loop."""

    artifact: str

    def read(self) -> str: ...

    def validate(self, text: str) -> list[str]: ...

    def produce(self, case: EvalCase, text: str) -> str: ...

    def write(self, text: str, version: int) -> Path: ...

    def rewrite_prompt(
        self,
        text: str,
        weak: Sequence[TrialResult],
        *,
        cases: Sequence[EvalCase],
        agg: Aggregate,
    ) -> str: ...


@dataclass(frozen=True)
class SpecNode:
    """Um nó da spec de workflow, já resolvido para a impl que ele executa."""

    id: str
    impl: str


@dataclass
class SkillTunable:
    """Skill: texto vira comportamento só através de um modelo.

    `model`/`max_usd` moram no adapter (e não na assinatura do `produce`)
    porque o loop não decide política de custo por caso — decide uma vez, no
    `run_tune`, e o adapter carrega.
    """

    artifact: str
    model: str | None = None
    max_usd: float = 0.25
    root: Path | str | None = None

    def read(self) -> str:
        return (root_dir(self.root) / self.artifact).read_text(encoding="utf-8")

    def validate(self, text: str) -> list[str]:
        try:
            meta = research._parse_skill(text)
        except (research.ResearchError, ValueError) as e:
            return [f"skill ilegível: {e}"]
        if not meta:
            return ["skill sem frontmatter útil (name/kinds/description)"]
        return []

    def produce(self, case: EvalCase, text: str) -> str:
        # Import tardio: `tune` importa este módulo. E é o seam de teste — o
        # stub troca o atributo no MÓDULO, então a busca tem que ser em
        # call-time, nunca um `from ... import _run_case` no topo.
        from harness.improve import tune

        return tune._run_case(text, case, model=self.model, max_usd=self.max_usd)

    def write(self, text: str, version: int) -> Path:
        """Grava a skill no formato canônico. `version` fica na cadeia, não no
        arquivo: o artefato é o vencedor, e carimbar a versão dentro dele mudaria
        o `artifact_sha256` por um motivo que não é conteúdo."""
        meta = research._parse_skill(text)
        slug = str(meta.get("name") or PurePosixPath(self.artifact).stem)
        kinds = meta.get("kinds") or ["code"]
        desc = str(meta.get("description") or slug)
        proposal = research.ResearchProposal(
            topic=desc.removeprefix(_DESC_PREFIX),
            kind=str(kinds[0]),
            slug=slug,
            target_file=self.artifact,
        )
        path = root_dir(self.root) / self.artifact
        research._write_new(path, research.render_skill(proposal, _skill_body(text)))
        return path

    def rewrite_prompt(
        self,
        text: str,
        weak: Sequence[TrialResult],
        *,
        cases: Sequence[EvalCase],
        agg: Aggregate,
    ) -> str:
        return _rewrite_prompt(
            "skill (frontmatter TOML entre '---' e corpo markdown)",
            text,
            weak,
            cases=cases,
            agg=agg,
        )


@dataclass
class WorkflowTunable:
    """Workflow: a saída de um caso é função pura da spec — zero LLM na medição.

    Render determinístico e não execução do grafo de propósito: rodar o workflow
    de verdade para medir custaria backend, tempo e um sandbox por trial, e o que
    o exame julga aqui é a ESTRUTURA declarada (quais nós, em que impl), que é
    exatamente o que a spec pode mudar.
    """

    artifact: str
    model: str | None = None
    max_usd: float = 0.25
    root: Path | str | None = None

    def read(self) -> str:
        return (root_dir(self.root) / self.artifact).read_text(encoding="utf-8")

    def validate(self, text: str) -> list[str]:
        try:
            spec = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return [f"toml torto: {e}"]
        try:
            topology.compile_spec(spec)
        except topology.TopologyError as e:
            return [str(e)]
        return []

    def produce(self, case: EvalCase, text: str) -> str:
        nodes = spec_nodes(text)
        head = f"# caso: {case.prompt}"
        return "\n".join([head, *(f"# {n.id}: {n.impl}" for n in nodes)]) + "\n"

    def write(self, text: str, version: int) -> Path:
        path = root_dir(self.root) / self.artifact
        workflow_action._write_new(path, text)
        return path

    def rewrite_prompt(
        self,
        text: str,
        weak: Sequence[TrialResult],
        *,
        cases: Sequence[EvalCase],
        agg: Aggregate,
    ) -> str:
        whitelist = ", ".join(sorted(topology.NODE_IMPLS))
        return _rewrite_prompt(
            f"workflow em TOML (`nodes` e `edges`; nós válidos: {whitelist})",
            text,
            weak,
            cases=cases,
            agg=agg,
        )


def spec_nodes(text: str) -> list[SpecNode]:
    """Os nós declarados, na ordem da spec, com o nome da impl que executam.

    Fora do `validate` de propósito: quem chama isto já validou, e um `.get`
    tolerante aqui evita duplicar a mensagem de erro da topologia.
    """
    spec = tomllib.loads(text)
    out: list[SpecNode] = []
    for name in spec.get("nodes") or ():
        impl = topology.NODE_IMPLS.get(str(name))
        out.append(SpecNode(id=str(name), impl=getattr(impl, "__name__", "?")))
    return out


def tunable_for(
    artifact: str,
    *,
    model: str | None = None,
    max_usd: float = 0.25,
    root: Path | str | None = None,
) -> Tunable:
    """O adapter pelo PREFIXO do path — mesma regra do `bundle_dir`.

    Prefixo e não extensão porque é o diretório que diz o que a coisa é: um
    `.md` em `config/workflows/` não seria uma skill, seria um erro.
    """
    key = PurePosixPath(Path(artifact).as_posix()).as_posix()
    kw = {"model": model, "max_usd": max_usd, "root": root}
    if key.startswith(WORKFLOWS_PREFIX):
        return WorkflowTunable(artifact=key, **kw)
    if key.startswith(SKILLS_PREFIX):
        return SkillTunable(artifact=key, **kw)
    raise ValueError(
        f"artefato sem adapter de tuning: {artifact!r} "
        f"(esperado prefixo {SKILLS_PREFIX!r} ou {WORKFLOWS_PREFIX!r})"
    )


def _case_prompt(text: str, case: EvalCase) -> str:
    """O prompt de UM caso: a skill como orientação, o caso como pedido."""
    return (
        "Você responde seguindo ESTRITAMENTE a orientação abaixo.\n\n"
        f"--- orientação ---\n{text.strip()}\n--- fim ---\n\n"
        f"Pedido (kind={case.kind}): {case.prompt}\n"
        "Responda em markdown, direto, sem preâmbulo."
    )


def _rewrite_prompt(
    shape: str,
    text: str,
    weak: Sequence[TrialResult],
    *,
    cases: Sequence[EvalCase],
    agg: Aggregate,
) -> str:
    """O prompt da reescrita, em três blocos: como se mede, o que reprovou, onde
    não regredir.

    `caso:eixo` sozinho não é feedback: nomeia o sintoma e esconde o dado. Os
    três blocos existem porque o reescritor erra de três formas distintas —
    escreve para o humano gostar do texto (e não para a RESPOSTA carregar a
    âncora literal), corrige o eixo errado, ou paga um eixo já em 100% para
    comprar decimal em outro. Um bloco para cada.
    """
    return (
        f"Reescreva o artefato abaixo, que é um {shape}.\n\n"
        f"--- artefato atual ---\n{text.strip()}\n--- fim ---\n\n"
        f"{_measured_block(cases)}\n\n"
        f"{_failures_block(weak)}\n\n"
        f"{_scoreboard_block(agg)}\n\n"
        "Corrija exatamente essas reprovações preservando o que já passa. "
        "Responda APENAS com o artefato completo reescrito, sem comentário em volta."
    )


def _measured_block(cases: Sequence[EvalCase]) -> str:
    """O bloco (i): a régua explicada, e as âncoras literais caso a caso."""
    lines = [
        "## Como você é medido",
        "",
        "O texto que você escreve NÃO é pontuado. Pontuada é a RESPOSTA que um modelo",
        "produz seguindo esse texto, um pedido de cada vez. Para um eixo passar, a",
        "orientação precisa fazer a RESPOSTA conter as strings LITERAIS abaixo — a",
        "comparação é por substring, sem sinônimo, sem paráfrase e sem tradução:",
        "",
    ]
    for c in cases:
        lines.append(f"- {c.id} ({c.kind}) — pedido: {c.prompt}")
        lines += [f"  - {req}" for req in _case_requirements(c)]
    return "\n".join(lines)


def _case_requirements(case: EvalCase) -> list[str]:
    expect = case.expect or {}
    reqs: list[str] = []
    must = [str(m) for m in expect.get("must_mention") or ()]
    if must:
        reqs.append("a resposta precisa conter: " + ", ".join(f'"{m}"' for m in must))
    nao = [str(m) for m in expect.get("must_not_mention") or ()]
    if nao:
        reqs.append("a resposta NÃO pode conter: " + ", ".join(f'"{m}"' for m in nao))
    if expect.get("must_refuse"):
        reqs.append('a resposta precisa RECUSAR em palavras ("não posso", "recuso", "não vou")')
    if case.verify_cmd:
        reqs.append(f"comando rodado sobre a resposta, precisa sair 0: `{case.verify_cmd}`")
    if not reqs:
        reqs.append("sem âncora literal; vale a forma (título ou lista, sem repetição)")
    return reqs


def _failures_block(weak: Sequence[TrialResult]) -> str:
    """O bloco (ii): agrupado por caso, com a nota do `diagnose` por eixo.

    Dedup por (caso, eixo, nota): N trials do mesmo caso reprovam pelo mesmo
    motivo, e repetir a linha N vezes ensinaria o reescritor que aquele caso
    pesa N vezes mais — o peso já está no placar.
    """
    por_caso: dict[str, list[str]] = {}
    for r in weak:
        for axis, ok in sorted(r.axes.items()):
            if ok:
                continue
            linha = f"  - {axis}: {r.notes.get(axis) or '(sem diagnóstico)'}"
            bucket = por_caso.setdefault(r.case_id, [])
            if linha not in bucket:
                bucket.append(linha)
    if not por_caso:
        return "## Reprovações concretas\n\n- (nenhuma falha isolada; melhore a margem)"
    lines = ["## Reprovações concretas", ""]
    for cid, notas in sorted(por_caso.items()):
        lines.append(f"- {cid}")
        lines += notas
    return "\n".join(lines)


def _scoreboard_block(agg: Aggregate) -> str:
    """O bloco (iii): onde já se ganhou. Eixo em 100% é patrimônio, não folga."""
    lines = ["## Placar por eixo (succ/n, piso de Wilson)", ""]
    lines += [
        f"- {axis} {succ}/{n} ({agg.lower.get(axis, 0.0):.3f})"
        for axis, (succ, n) in sorted(agg.per_axis.items())
    ]
    cheios = [axis for axis, (succ, n) in sorted(agg.per_axis.items()) if n and succ == n]
    aviso = (
        f"EIXOS JÁ EM 100%: {', '.join(cheios)} — não regrida neles; perda aqui "
        "custa mais que ganho em outro eixo."
        if cheios
        else "EIXOS JÁ EM 100%: nenhum — todo eixo ainda tem folga para subir."
    )
    return "\n".join([*lines, "", aviso])


def _skill_body(text: str) -> str:
    """O corpo da skill, sem o frontmatter — `render_skill` recoloca o dele."""
    lines = text.splitlines()
    if lines and lines[0] == "---" and "---" in lines[1:]:
        end = lines[1:].index("---") + 2
        return "\n".join(lines[end:]).strip()
    return text.strip()
