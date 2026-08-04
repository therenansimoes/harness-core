"""Ação 'procedural': skill de PROCEDIMENTO minerada dos traces de tool.

O `dream` destila da memória episódica, que só guarda FALHA. Sobra o outro
lado: os runs que deram certo deixaram no disco a sequência de ferramentas que
levou até o verde (`data/logs/<run_id>/trace.<attempt>.jsonl` desde V3.0), e
ninguém lê. Essa sequência é conhecimento procedural — não "o que evitar", mas
"em que ordem mexer".

O SINAL É LIFT, NÃO FREQUÊNCIA. `read_file` aparece em todo run aceito e também
em todo run que falhou; n-grama igualmente comum nos dois corpora não ensina
nada, e uma skill que manda "leia arquivos" é ruído puro competindo por prompt.
Então cada n-grama é medido duas vezes — cobertura nos aceitos e cobertura nos
falhos — e só passa quem tem suporte `MIN_SUPPORT` E `lift >= MIN_LIFT`.

Duas decisões que parecem detalhe e não são:

- N-grama de NOMES de tool, sem argumento. Argumento (path, texto do edit) é
  exatamente o que faz dois runs com o mesmo procedimento parecerem dois
  procedimentos; o nome é a parte reusável.
- Skill no formato do `research.render_skill` via `dream._skill_body`-style: dois
  renderizadores de skill é como os formatos divergiriam (mesmo argumento que o
  `dream` usa para reusar o `research`). A candidata entra no ciclo normal de
  attribution/lift/prune — a mineração propõe, o placar decide.

Fail-open na leitura (trace é diagnóstico: linha inválida, arquivo ilegível e
json torto são IGNORADOS, nunca levantam) e fail-closed na escrita (`propose`
devolve None sem material; `apply` valida genoma e skill antes de tocar disco,
mesmo contrato do `mutate.apply`).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from harness.genome.genome import Genome
from harness.improve import mutate, research, root_dir
from harness.ledger import store
from harness.ruler.verify import LOGS_REL

if TYPE_CHECKING:
    from harness.improve.target import Action

ACTION = "procedural"

# Tamanho dos n-gramas. 1 é "usou read_file", que não é procedimento; acima de 4
# o padrão vira a transcrição de um run só e nunca tem suporte.
MIN_N = 2
MAX_N = 4

# Runs ACEITOS DISTINTOS que precisam conter o n-grama. Três pelo motivo do
# `dream.MIN_RECURRENT`: dois é coincidência (ou o mesmo run em duas tentativas).
MIN_SUPPORT = 3

# Cobertura nos aceitos dividida pela cobertura nos falhos. 1.5 = o padrão
# aparece 50% mais nos aceitos; abaixo disso ele descreve o backend, não o
# sucesso.
MIN_LIFT = 1.5

# Sem corpus de falha não há denominador. Tratar "nenhum falho" como lift
# infinito faria a primeira semana do projeto propor skill de qualquer coisa —
# este piso é o denominador mínimo, não uma contagem real.
_NO_FAIL_FLOOR = 1.0

# Runs lidos por mineração. Teto de I/O: cada run é um diretório com N arquivos.
DEFAULT_LIMIT = 200


class ProceduralError(Exception):
    """Mineração inválida — nada foi escrito."""


@dataclass(frozen=True)
class Pattern:
    """Um n-grama vencedor com o placar que o sustenta. `run_ids` é o paper
    trail: sem ele o lift é uma afirmação sobre o disco que ninguém confere."""

    tools: tuple[str, ...]
    kind: str
    support: int
    fail_support: int
    ok_runs: int
    fail_runs: int
    lift: float
    run_ids: tuple[str, ...]

    @property
    def label(self) -> str:
        return " → ".join(self.tools)


@dataclass(frozen=True)
class ProceduralProposal:
    """A candidata (no máx. uma) mais os rivais que ela venceu.

    `skill` é o que o `mutate.check` julga — mesmo contrato duck-typed das outras
    ações.
    """

    skill: research.ResearchProposal
    skill_body: str
    pattern: Pattern
    runners_up: tuple[Pattern, ...] = ()
    scanned: int = 0

    @property
    def target_file(self) -> str:
        return self.skill.target_file


@dataclass(frozen=True)
class ProceduralRecord:
    """O que a mineração fez. Só `str`/`int`/`float`, pelo motivo da `Mutation`:
    atravessa o checkpoint como dict inerte sem perder informação."""

    skill_path: str
    pattern: str
    kind: str
    support: int
    lift: float
    written_at: str


def logs_dir(data_dir: Path | str | None = None) -> Path:
    """`<data_dir>/logs`, o mesmo diretório que o `run_log_dir` da régua escreve.
    Sem `mkdir`: leitor não cria o que devia ter sido escrito por outro."""
    base = Path(data_dir) if data_dir is not None else store.data_dir()
    return base / LOGS_REL


def tool_sequence(run_id: str, data_dir: Path | str | None = None) -> tuple[str, ...]:
    """Os nomes de tool do run, na ordem, concatenando as tentativas.

    Tentativa 2 continua o mesmo procedimento da 1 (o agente reagiu ao verify),
    então o arquivo por tentativa é detalhe de armazenamento — o que se minera é
    o caminho até o veredito. Ordena por nome de arquivo para `trace.1` vir antes
    de `trace.2`.
    """
    run_dir = logs_dir(data_dir) / run_id
    if not run_dir.is_dir():
        return ()
    out: list[str] = []
    for path in sorted(run_dir.glob("trace.*.jsonl")):
        out.extend(_tools_in_trace(path))
    return tuple(out)


def _tools_in_trace(path: Path) -> list[str]:
    """Nomes de tool de UM arquivo de trace, tolerando lixo em qualquer nível.

    Trace é diagnóstico best-effort do backend (ver `_write_trace`): arquivo
    truncado no meio de uma linha, linha de erro, linha de todos e formato de
    backend diferente convivem no mesmo diretório. Cada nível de defeito é
    ignorado no nível dele — a linha ruim não invalida o arquivo, e o arquivo
    ruim não invalida a mineração.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # linha corrompida: ignora a linha, não o trace
        if isinstance(obj, dict):
            out.extend(_tool_names(obj))
    return out


def _tool_names(obj: dict) -> list[str]:
    """`tool_calls` do nosso `_write_trace` OU os blocos `tool_use` do formato
    stream-json do CLI. Dois formatos porque dois backends escrevem trace, e
    minerar só um deles esconderia metade do corpus."""
    out: list[str] = []
    calls = obj.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else call
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else obj.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def ngrams(tools: Sequence[str], min_n: int = MIN_N, max_n: int = MAX_N) -> set[tuple[str, ...]]:
    """Os n-gramas DISTINTOS da sequência.

    Set, não lista: suporte se conta em RUNS, não em ocorrências — um run que
    repete `read_file → edit_range` doze vezes não é doze evidências.
    """
    out: set[tuple[str, ...]] = set()
    for n in range(min_n, max_n + 1):
        for i in range(len(tools) - n + 1):
            out.add(tuple(tools[i : i + n]))
    return out


def _coverage(runs: Iterable[tuple[str, tuple[str, ...]]]) -> tuple[Counter, dict, int]:
    """Cobertura por n-grama: em quantos runs ele aparece, e em quais.

    Devolve também o total de runs COM sequência — run sem tool nenhum (trace
    ausente, backend que morreu no preflight) não pode entrar no denominador do
    lift, senão o corpus de falha se dilui e tudo vira 'padrão de sucesso'.
    """
    counts: Counter = Counter()
    witnesses: dict[tuple[str, ...], list[str]] = {}
    total = 0
    for run_id, tools in runs:
        grams = ngrams(tools)
        if not grams:
            continue
        total += 1
        for gram in grams:
            counts[gram] += 1
            witnesses.setdefault(gram, []).append(run_id)
    return counts, witnesses, total


def mine(
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    min_support: int = MIN_SUPPORT,
    min_lift: float = MIN_LIFT,
    data_dir: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[list[Pattern], int]:
    """Os padrões que passam suporte E lift, mais forte primeiro.

    Empate desempata por n-grama mais LONGO e depois alfabético, pelo motivo de
    sempre (duas minerações com a mesma evidência propõem a mesma skill) e por
    um segundo: entre `ls → read_file` e `ls → read_file → edit_range` com o
    mesmo placar, o mais longo diz mais.
    """
    path = Path(db_path) if db_path is not None else None
    rows = store.history(kind=kind, limit=limit, path=path)
    ok_runs = [(r.run_id, tool_sequence(r.run_id, data_dir)) for r in rows if r.ok]
    fail_runs = [(r.run_id, tool_sequence(r.run_id, data_dir)) for r in rows if not r.ok]
    ok_counts, witnesses, ok_total = _coverage(ok_runs)
    fail_counts, _, fail_total = _coverage(fail_runs)
    scanned = ok_total + fail_total
    if ok_total == 0:
        return [], scanned

    kinds = Counter(r.kind for r in rows if r.ok and r.kind)
    resolved_kind = kind or (kinds.most_common(1)[0][0] if kinds else "")

    found: list[Pattern] = []
    for gram, support in ok_counts.items():
        if support < min_support:
            continue
        fail_support = fail_counts.get(gram, 0)
        ok_cov = support / ok_total
        # Denominador: cobertura nos falhos, com piso de "um run falho que teria
        # o padrão" quando o corpus de falha é pequeno ou vazio.
        fail_cov = max(
            fail_support / fail_total if fail_total else 0.0,
            _NO_FAIL_FLOOR / max(fail_total, min_support),
        )
        lift = ok_cov / fail_cov
        if lift < min_lift:
            continue
        found.append(
            Pattern(
                tools=gram,
                kind=resolved_kind,
                support=support,
                fail_support=fail_support,
                ok_runs=ok_total,
                fail_runs=fail_total,
                lift=round(lift, 3),
                run_ids=tuple(sorted(witnesses.get(gram, ()))),
            )
        )
    found.sort(key=lambda p: (-p.lift, -p.support, -len(p.tools), p.tools))
    return found, scanned


def propose_procedural(
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    min_support: int = MIN_SUPPORT,
    min_lift: float = MIN_LIFT,
    data_dir: Path | str | None = None,
    db_path: Path | str | None = None,
) -> ProceduralProposal | None:
    """A candidata do padrão mais forte, ou None.

    None é o mesmo contrato do `pick_target`/`propose_dream`: sem suporte ou sem
    lift não há procedimento provado, e "não há procedimento" nunca é "escreva
    uma skill genérica".
    """
    found, scanned = mine(
        kind=kind,
        limit=limit,
        min_support=min_support,
        min_lift=min_lift,
        data_dir=data_dir,
        db_path=db_path,
    )
    if not found:
        return None
    best = found[0]
    skill, body = _skill(best)
    return ProceduralProposal(
        skill=skill,
        skill_body=body,
        pattern=best,
        runners_up=tuple(found[1:6]),
        scanned=scanned,
    )


def _skill(pattern: Pattern) -> tuple[research.ResearchProposal, str]:
    """A candidata, no formato do `research` — `slugify` do research de propósito,
    duas rotinas de slug é como os dois formatos de skill divergiriam."""
    chain = "-".join(pattern.tools)
    topic = f"procedimento {pattern.label}".strip()
    slug = research.slugify(f"proc {pattern.kind} {chain}")
    proposal = research.ResearchProposal(
        topic=topic,
        kind=pattern.kind,
        slug=slug,
        target_file=f"{research.SKILLS_SUBDIR}/{slug}.md",
        reasons=(
            f"procedural:support({pattern.support}/{pattern.ok_runs})",
            f"lift:{pattern.lift}",
            f"runs:{','.join(pattern.run_ids[:5])}",
        ),
    )
    return proposal, _skill_body(pattern)


def _skill_body(pattern: Pattern) -> str:
    """Corpo honesto (mesmo argumento do `dream._skill_body`): a evidência é o
    placar, e o placar está escrito no corpo — incluindo o lado que enfraquece a
    skill (quantos runs FALHOS também tinham o padrão)."""
    steps = "\n".join(f"{i}. `{tool}`" for i, tool in enumerate(pattern.tools, 1))
    runs = ", ".join(pattern.run_ids[:5]) or "(sem run_id)"
    return (
        f"Padrão minerado dos traces de tool: `{pattern.label}` apareceu em "
        f"{pattern.support} de {pattern.ok_runs} runs ACEITOS e em "
        f"{pattern.fail_support} de {pattern.fail_runs} runs falhos "
        f"(lift {pattern.lift}).\n\n"
        f"## Quando usar\n\nEm unidades kind={pattern.kind or '?'}, quando a tarefa "
        "envolver mexer em arquivo que já existe: este é o caminho que os runs "
        "aceitos percorreram, e os que falharam não.\n\n"
        f"## O procedimento\n\n{steps}\n\n"
        "Não pule etapa para economizar turno: a ordem é o que tem lift, não as "
        "ferramentas em si — cada uma delas isolada aparece igual nos dois "
        "corpora.\n\n"
        f"## Evidência\n\n- Runs aceitos com o padrão: {runs}\n"
        f"- Suporte: {pattern.support} run(s) aceito(s) distinto(s)\n"
        f"- Lift vs. corpus de falha: {pattern.lift} (mínimo exigido: {MIN_LIFT})\n\n"
        "> Candidata minerada, não conhecimento validado: correlação com run "
        "aceito não é causa, e o lift real desta skill ainda vai ser medido pelo "
        "ciclo de attribution/prune."
    )


def apply_procedural(
    proposal: ProceduralProposal,
    root: Path | str | None = None,
    genome: Genome | None = None,
    now: datetime | str | None = None,
) -> ProceduralRecord:
    """Valida genoma e skill ANTES de escrever, e só então toca o disco.

    Mesmo fail-closed do `mutate.apply`/`apply_dream`, mesma exceção, para o
    chamador tratar REJECTED por um caminho só.
    """
    if not proposal.skill_body.strip():
        raise ProceduralError("procedimento sem corpo: skill que não ensina nada")
    if len(proposal.pattern.tools) < MIN_N:
        raise ProceduralError(f"padrão de {len(proposal.pattern.tools)} tool não é procedimento")

    base = root_dir(root)
    violations = mutate.check(proposal.skill, root=base, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)
    text = research.render_skill(proposal.skill, proposal.skill_body)
    # cinto e suspensório do research: skill ilegível não chega ao disco.
    research._parse_skill(text)
    research._write_new(base / proposal.skill.target_file, text)
    return ProceduralRecord(
        skill_path=proposal.skill.target_file,
        pattern=proposal.pattern.label,
        kind=proposal.pattern.kind,
        support=proposal.pattern.support,
        lift=proposal.pattern.lift,
        written_at=_clock(now).isoformat(timespec="seconds"),
    )


def _clock(now: datetime | str | None) -> datetime:
    """Agora, aware em UTC — o relógio é injetável pelo motivo do `dream._clock`:
    o registro é auditado, e timestamp escondido dentro da função não é
    reproduzível."""
    from harness.improve import dream

    return dream._clock(now)


def action() -> Action:
    """A ação registrável — consultada por `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_procedural, apply=apply_procedural)
