"""Ação 'redteam': o harness tenta quebrar as próprias skills e prompts.

`research` destila conhecimento novo; `synthesize` colhe a falha que já
aconteceu. O red-team ataca o terceiro flanco: a instrução vigente que está
ERRADA e ninguém notou porque nenhum exame a exercita. O backend lê as skills
de `skills/*.md` e o prompt de `prompts/executor.md` e devolve contra-exemplos
— tarefas onde seguir a instrução ao pé da letra leva a resposta errada ou
ambígua.

Contra-exemplo não é veredito: cada um vira exame CANDIDATO em
`benchmarks/quarantine/`, no formato exato do `synthesize` (mesmo `unit.toml`,
mesmo `[origin]`), porque promover para `sealed` continua sendo ato humano —
uma ação que pudesse escrever a prova escreveria a própria nota.

Fail-closed nos dois tempos, pelo motivo do `mutate.apply`: saída de backend
inválida não vira exame pela metade (`propose` devolve None, no-op limpo), e o
`apply` recusa o LOTE inteiro se qualquer spec não sobreviver à validação
estrutural — nada toca o disco antes de tudo ter passado.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from harness.backends.registry import get_backend
from harness.genome.genome import Genome
from harness.improve import mutate, root_dir
from harness.improve import synthesize as synth
from harness.types import ExecRequest

if TYPE_CHECKING:
    from harness.improve.target import Action

ACTION = "redteam"

SKILLS_SUBDIR = Path("skills")
EXECUTOR_PROMPT = Path("prompts") / "executor.md"
UNIT_FILE = "unit.toml"

# Contra-exemplos pedidos por ciclo. Pequeno de propósito: cada spec que passa
# vira exame que alguém vai ter que revisar antes de selar.
DEFAULT_N = 3

# `exit_reason` do `[origin]`: no formato do synthesize a origem é a run que
# falhou; aqui não houve run, o que "falhou" foi a instrução vigente.
ORIGIN_REASON = "redteam"

_REQUIRED = ("id", "prompt", "verify_cmd")


class RedteamError(Exception):
    """Spec adversarial inválida — nada foi escrito."""


@dataclass(frozen=True)
class AdversarialSpec:
    """Um contra-exemplo. `target_file` é o que o genome_check julga — mesmo
    contrato duck-typed do `mutate.check`."""

    id: str
    prompt: str
    verify_cmd: str
    kind: str | None
    slug: str
    target_file: str
    attacks: str = ""


@dataclass(frozen=True)
class RedteamProposal:
    """O lote de contra-exemplos e de onde veio. `sources` é o resumo
    auditável: red-team sem alvo declarado não é revisável."""

    specs: tuple[AdversarialSpec, ...]
    backend: str
    sources: tuple[str, ...] = ()


def read_targets(root: Path | str | None = None) -> tuple[str, tuple[str, ...]]:
    """Instruções vigentes em texto + os paths que as produziram.

    Skills pelo loader oficial (o mesmo que o run injeta — atacar um parse
    próprio seria atacar instrução que ninguém executa). Skill malformada o
    loader já pula; prompt ausente não é erro, só menos superfície.
    """
    from harness.skills.loader import load_skills

    base = root_dir(root)
    parts: list[str] = []
    sources: list[str] = []
    for skill in load_skills(base / SKILLS_SUBDIR):
        parts.append(f"### skill {skill.name} (kinds={list(skill.kinds)})\n{skill.body}")
        sources.append(skill.path.relative_to(base).as_posix())

    prompt_file = base / EXECUTOR_PROMPT
    if prompt_file.is_file():
        text = prompt_file.read_text(encoding="utf-8").strip()
        if text:
            parts.append(f"### prompt {EXECUTOR_PROMPT.as_posix()}\n{text}")
            sources.append(EXECUTOR_PROMPT.as_posix())
    return "\n\n".join(parts), tuple(sources)


def redteam_prompt(targets: str, n: int = DEFAULT_N) -> str:
    """Pede spec executável, não crítica literária: o produto é exame com
    `verify_cmd` que roda, senão a quarentena enche de opinião."""
    return (
        "Você é o red-team destas instruções vigentes de um harness de agentes."
        f" Proponha {n} tarefas ADVERSARIAIS: casos em que seguir a instrução"
        " ao pé da letra leva a resposta errada, ambígua ou incompleta.\n\n"
        f"{targets}\n\n"
        "Responda SÓ com um array JSON. Cada item:"
        ' {"id": "slug-curto", "kind": "code", "prompt": "a tarefa",'
        ' "verify_cmd": "comando shell que falha se a resposta estiver errada",'
        ' "attacks": "qual instrução o caso quebra"}.'
        " Sem markdown, sem preâmbulo."
    )


def propose_redteam(
    backend: str = "mock",
    n: int = DEFAULT_N,
    root: Path | str | None = None,
    model: str | None = None,
    timeout_s: float = 600.0,
) -> RedteamProposal | None:
    """Roda o red-team e devolve o lote, ou None.

    None é o mesmo contrato do `pick_target`: sem instrução para atacar, com
    backend que falhou ou com saída que não é um lote de specs válidas, não há
    proposta — e "não há proposta" nunca é "escreva o que der". Uma spec
    inválida no meio invalida o lote: aproveitar metade da saída de um backend
    que já se mostrou confuso é escolher em qual metade confiar.
    """
    targets, sources = read_targets(root)
    if not targets:
        return None

    workspace = root_dir(root) / "data" / "redteam"
    result = get_backend(backend).execute(
        ExecRequest(
            prompt=redteam_prompt(targets, n),
            workspace=workspace,
            model=model,
            timeout_s=timeout_s,
            trace_path=workspace / "trace.jsonl",
        )
    )
    if not result.ok:
        return None

    try:
        raw = _parse_output(workspace, result.files_changed)
        specs = tuple(_spec(item) for item in raw)
    except RedteamError:
        return None
    if not specs:
        return None
    # dedupe por slug: dois contra-exemplos no mesmo dir seriam um sobrescrevendo
    # o outro, e o lote deixaria de dizer o que foi proposto.
    by_slug = {s.slug: s for s in reversed(specs)}
    ordered = tuple(s for s in specs if by_slug[s.slug] is s)
    return RedteamProposal(specs=ordered, backend=backend, sources=sources)


def apply_redteam(
    proposal: RedteamProposal,
    root: Path | str | None = None,
    genome: Genome | None = None,
) -> list[Path]:
    """Genoma → render → roundtrip → escrita, nessa ordem e por LOTE.

    O genoma vem primeiro e para todas as specs: `benchmarks/quarantine/**` é
    mutável, `sealed/**` não é, e é o genoma que garante que uma spec com id
    torto não escreva fora da quarentena. O roundtrip é o cinto e suspensório
    do `apply_evolve`: unit que o parser oficial não lê não chega ao disco.
    Dir de quarentena já existente é pulado, mesmo dedupe do `synthesize`.
    """
    base = root_dir(root)
    if not proposal.specs:
        raise RedteamError("lote vazio: nada para materializar")

    violations: list[str] = []
    for spec in proposal.specs:
        violations += mutate.check(spec, root=base, genome=genome)
    if violations:
        raise mutate.GenomeViolation(list(dict.fromkeys(violations)))

    # renderiza tudo ANTES de escrever qualquer coisa: lote recusado no meio
    # deixaria quarentena com metade do ataque, que é pior que nenhum.
    rendered: list[tuple[Path, str]] = []
    for spec in proposal.specs:
        text = render_unit(spec, proposal.backend)
        _validate_unit(text, spec)
        rendered.append((base / spec.target_file, text))

    created: list[Path] = []
    for path, text in rendered:
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        created.append(path.parent)
    return created


def render_unit(spec: AdversarialSpec, backend: str = "mock") -> str:
    """Formato do `synthesize`, campo por campo — o exame de red-team é lido
    pelo mesmo `cli.load_unit` e pelo mesmo `harness seal`."""
    # `synth._s`/`synth._slug` são privados, mas ter uma segunda rotina de
    # string->TOML (ou de slug) aqui é justamente como os dois formatos
    # divergiriam sem ninguém notar — mesmo argumento do `mutate._render`
    # importado pelo research.
    lines = [
        "# Contra-exemplo do red-team interno — quarentena, não selado.",
        f"# Origem: redteam via backend {backend}"
        + (f"; ataca: {spec.attacks.splitlines()[0]}" if spec.attacks else "")
        + ".",
        f"id = {synth._s(spec.id)}",
    ]
    if spec.kind is not None:
        lines.append(f"kind = {synth._s(spec.kind)}")
    lines += [
        f"prompt = {synth._s(spec.prompt)}",
        f"verify_cmd = {synth._s(spec.verify_cmd)}",
        "",
        "[origin]",
        f"run_id = {synth._s(f'{ORIGIN_REASON}:{backend}')}",
        f"exit_reason = {synth._s(ORIGIN_REASON)}",
    ]
    return "\n".join(lines) + "\n"


def _validate_unit(text: str, spec: AdversarialSpec) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RedteamError(f"unit não é TOML válido: {spec.slug} ({e})") from e
    missing = [k for k in _REQUIRED if not str(data.get(k, "")).strip()]
    if missing:
        raise RedteamError(f"unit incompleta: {spec.slug} (falta {', '.join(missing)})")


def _parse_output(workspace: Path, files_changed: Sequence[str]) -> list[Any]:
    """Lote = o JSON que o backend deixou no workspace. Aceita array no topo ou
    `{"specs": [...]}`; qualquer outra coisa é saída inválida, não lote vazio."""
    items: list[Any] = []
    for name in files_changed:
        p = workspace / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RedteamError(f"saída não é JSON: {p} ({e})") from e
        if isinstance(data, dict):
            data = data.get("specs")
        if not isinstance(data, list):
            raise RedteamError(f"saída não é lote de specs: {p}")
        items += data
    if not items:
        raise RedteamError(f"red-team vazio: nenhuma spec em {workspace}")
    return items


def _spec(item: Any) -> AdversarialSpec:
    if not isinstance(item, dict):
        raise RedteamError(f"spec não é tabela: {item!r}")
    values: dict[str, str] = {}
    for key in _REQUIRED:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RedteamError(f"spec sem {key}: {item!r}")
        values[key] = value.strip()
    slug = synth._slug(values["id"])
    kind = item.get("kind")
    attacks = item.get("attacks")
    return AdversarialSpec(
        id=values["id"],
        prompt=values["prompt"],
        verify_cmd=values["verify_cmd"],
        kind=str(kind) if isinstance(kind, str) and kind.strip() else None,
        slug=slug,
        target_file=f"{synth.QUARANTINE_DIR.as_posix()}/{slug}/{UNIT_FILE}",
        attacks=str(attacks).strip() if isinstance(attacks, str) else "",
    )


def action() -> "Action":
    """A ação registrável — consultada por `target.actions()` quando o wiring
    do registry chegar."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_redteam, apply=apply_redteam)
