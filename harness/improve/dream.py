"""Ação 'dream': consolidação OFFLINE da memória episódica.

`research` busca conhecimento que falta; `redteam` ataca a instrução vigente.
O sono ataca o quarto flanco: a memória que só cresce. `episodic` indexa toda
falha e nunca esquece nada, então o `recall` vai ficando mais barulhento a cada
run — o caso único de três meses atrás disputa o prompt com a falha que está
acontecendo agora.

Duas operações, as duas sem LLM (é sono, não pesquisa: o material já está no
banco):

- FUNDIR os RECORRENTES — mesmo kind e mesma assinatura de trace, `MIN_RECURRENT`
  vezes ou mais. O grupo mais forte vira no máximo UMA skill candidata, no
  formato compartilhado do `research.render_skill`. Candidata é a palavra certa:
  skill é zona mutável do genoma e entra no ciclo normal de
  attribution/lift/prune — o sono propõe, o placar decide.
- ARQUIVAR os ÓRFÃOS — ocorrência única e mais velha que `ORPHAN_AGE_DAYS`.
  Arquivar é SOFT (`episodic.archive`), nunca DELETE: o episódio sai do `recall`
  e continua auditável no banco.

O relógio é injetável (`now`) pelo motivo do `sleep_fn` dos vigias: "mais velho
que 7 dias" é a decisão inteira desta ação, e decisão que depende de
`datetime.now()` escondido dentro da função não é testável nem reproduzível.

Fail-closed nos dois tempos, como o `mutate.apply`: sem material suficiente
`propose_dream` devolve None (no-op limpo), e o `apply_dream` valida genoma e
skill ANTES de tocar o disco — sono que arquiva órfão e depois falha ao escrever
a skill teria apagado memória em troca de nada.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from harness.genome.genome import Genome
from harness.improve import mutate, research, root_dir
from harness.ledger import store
from harness.memory import episodic

if TYPE_CHECKING:
    from harness.improve.target import Action

ACTION = "dream"
DREAMS_SUBDIR = "dreams"

# Ocorrências no mesmo (kind, assinatura) para o grupo contar como recorrente.
# Três é o menor número que distingue padrão de coincidência: dois episódios
# iguais ainda podem ser o mesmo run tentando duas vezes.
MIN_RECURRENT = 3

# Idade mínima do órfão. Falha única de ontem ainda é a falha de ontem — pode
# estar no meio de acontecer de novo.
ORPHAN_AGE_DAYS = 7

# Janela de leitura. Episódio mais velho que isto não entra na consolidação
# deste sono (nem como recorrente, nem como órfão): sono é incremental.
WINDOW_DAYS = 90

# Tokens da assinatura. Curto de propósito: assinatura longa nunca colide, e
# assinatura que nunca colide não funde nada.
SIG_TOKENS = 8

# Trecho citado no relatório e na skill. Menor que o MAX_TRACE_CHARS do
# episódio: o relatório é para o humano ler, não para reproduzir o log.
SAMPLE_CHARS = 400

# Só letras: número de linha, pid, hash e timestamp são exatamente o que faz a
# mesma falha parecer duas.
_SIG_TOKEN = re.compile(r"[a-z_]{3,}")
_WS = re.compile(r"\s+")

# Boilerplate do traceback Python. É idêntico em TODA falha de assert, então
# assinar por cima dele funde lições que não têm nada em comum — foi o primeiro
# sono deste repo fundindo localStorage, botão de tema e uma terceira unidade
# sob uma assinatura só.
_TB_NOISE = re.compile(
    r"""^(?:
        traceback\ \(most\ recent\ call\ last\)
      | file\ ".*",\ line\ \d+
      | line\ \d+,\ in\ .*
      | during\ handling\ of\ the\ above\ exception
      | the\ above\ exception\ was\ the\ direct\ cause
      | [~\ ]*\^+[~\ ]*$
    )""",
    re.VERBOSE,
)

# Cinto e suspensório: mesmo que uma linha de frame escape do filtro acima, as
# palavras dela não entram na assinatura.
_SIG_STOPWORDS = frozenset(
    ("traceback", "most", "recent", "call", "last", "file", "stdin", "line", "module")
)

# Assinatura de uma palavra só (`AssertionError` pelado) volta a fundir tudo:
# nesse caso a linha significativa anterior entra para desempatar.
_SIG_MIN_TOKENS = 2


class DreamError(Exception):
    """Consolidação inválida — nada foi arquivado nem escrito."""


@dataclass(frozen=True)
class Lesson:
    """Um grupo de recorrentes fundido. `episode_ids` é o paper trail: sem ele
    a lição é uma afirmação sobre o banco que ninguém consegue conferir."""

    kind: str
    signature: str
    n: int
    episode_ids: tuple[int, ...]
    unit_ids: tuple[str, ...]
    sample: str


@dataclass(frozen=True)
class Orphan:
    """Episódio único e velho, candidato a arquivamento soft."""

    id: int
    kind: str
    unit_id: str
    created_at: str
    sample: str


@dataclass(frozen=True)
class DreamProposal:
    """O que o sono viu. `skill` é a candidata (no máx. uma) e é ela que o
    `mutate.check` julga — mesmo contrato duck-typed das outras ações."""

    dream_report: str
    lessons: tuple[Lesson, ...] = ()
    orphans: tuple[Orphan, ...] = ()
    skill: research.ResearchProposal | None = None
    skill_body: str = ""

    @property
    def target_file(self) -> str:
        """Vazio quando o sono não destilou skill: só arquivamento e relatório,
        que não são zona do genoma."""
        return self.skill.target_file if self.skill is not None else ""


@dataclass(frozen=True)
class DreamRecord:
    """O que o sono fez. Só `str`/`int`, pelo motivo da `Mutation`: atravessa o
    checkpoint como dict inerte sem perder informação."""

    report_path: str
    skill_path: str
    archived: int
    lessons: int
    written_at: str


def _significant_lines(trace: str) -> list[str]:
    """As linhas da trace que dizem algo: sem vazias e sem frame de traceback."""
    out = []
    for raw in (trace or "").splitlines():
        line = raw.strip()
        if line and not _TB_NOISE.match(line.lower()):
            out.append(line)
    return out


def _tokens(text: str) -> list[str]:
    """Dedup preservando ordem: `AssertionError` repetido 20 vezes na trace não
    pode consumir a assinatura inteira."""
    found = (t for t in _SIG_TOKEN.findall(text.lower()) if t not in _SIG_STOPWORDS)
    return list(dict.fromkeys(found))


def signature(trace: str) -> str:
    """Assinatura textual do episódio: a mesma falha com outro número de linha,
    outro path e outro timestamp tem que colidir aqui, senão nada é recorrente.

    Assina sobre a ÚLTIMA linha significativa — em traceback Python é a do erro
    real (`AssertionError: tema nao persiste`), e é a única parte que distingue
    duas falhas. As anteriores só entram quando a última não dá token suficiente.
    """
    lines = _significant_lines(trace)
    tokens = _tokens(lines[-1]) if lines else []
    if len(tokens) < _SIG_MIN_TOKENS and len(lines) > 1:
        for extra in _tokens(" ".join(reversed(lines[:-1]))):
            if extra not in tokens:
                tokens.append(extra)
    if not tokens:  # trace só de boilerplate: melhor a assinatura antiga que nada
        tokens = _tokens(trace or "")
    return " ".join(tokens[:SIG_TOKENS])


def _sample(trace: str) -> str:
    return _WS.sub(" ", (trace or "").strip())[:SAMPLE_CHARS]


def _clock(now: datetime | str | None) -> datetime:
    """Agora, aware em UTC. `None` é o único caminho que consulta o relógio do
    processo — todo o resto da ação recebe o instante já decidido."""
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if isinstance(now, str):
        parsed = episodic.parse_ts(now)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def propose_dream(
    kind: str | None = None,
    now: datetime | str | None = None,
    window_days: int = WINDOW_DAYS,
    min_recurrent: int = MIN_RECURRENT,
    orphan_age_days: int = ORPHAN_AGE_DAYS,
    limit: int = episodic.DEFAULT_SCAN,
    db_path: Path | str | None = None,
) -> DreamProposal | None:
    """Agrupa a janela em lições e órfãos, ou devolve None.

    None é o mesmo contrato do `pick_target`: sem grupo recorrente e sem órfão
    velho não há consolidação a fazer — e "não há consolidação" nunca é
    "arquive algo". Empate no grupo mais forte desempata por kind e assinatura,
    pelo motivo de sempre: dois sonos com a mesma evidência sonham igual.
    """
    at = _clock(now)
    since = at - timedelta(days=window_days) if window_days > 0 else None
    path = Path(db_path) if db_path is not None else None
    eps = episodic.episodes(kind=kind, since=since, limit=limit, db_path=path)
    if not eps:
        return None

    groups: dict[tuple[str, str], list[episodic.Episode]] = {}
    for ep in eps:
        sig = signature(ep.trace)
        if not sig:
            continue  # trace sem token: não funde nem conta como órfão
        groups.setdefault((ep.kind, sig), []).append(ep)

    orphan_floor = at - timedelta(days=orphan_age_days)
    lessons: list[Lesson] = []
    orphans: list[Orphan] = []
    for (ep_kind, sig), members in groups.items():
        if len(members) >= min_recurrent:
            lessons.append(_lesson(ep_kind, sig, members))
        elif len(members) == 1:
            ep = members[0]
            ts = ep.timestamp
            if ts is not None and ts < orphan_floor:
                orphans.append(
                    Orphan(
                        id=ep.id,
                        kind=ep.kind,
                        unit_id=ep.unit_id,
                        created_at=ep.created_at,
                        sample=_sample(ep.trace),
                    )
                )

    if not lessons and not orphans:
        return None

    lessons.sort(key=lambda le: (-le.n, le.kind, le.signature))
    orphans.sort(key=lambda o: o.id)
    skill, body = _skill(lessons[0]) if lessons else (None, "")
    report = render_report(at, len(eps), window_days, tuple(lessons), tuple(orphans), skill)
    return DreamProposal(
        dream_report=report,
        lessons=tuple(lessons),
        orphans=tuple(orphans),
        skill=skill,
        skill_body=body,
    )


def _lesson(kind: str, sig: str, members: Sequence[episodic.Episode]) -> Lesson:
    """O trecho representativo é o do episódio MAIS RECENTE do grupo: a falha
    ainda viva descreve melhor o caso que a primeira vez que ela apareceu."""
    ordered = sorted(members, key=lambda e: e.id, reverse=True)
    return Lesson(
        kind=kind,
        signature=sig,
        n=len(members),
        episode_ids=tuple(sorted(e.id for e in members)),
        unit_ids=tuple(dict.fromkeys(e.unit_id for e in ordered if e.unit_id)),
        sample=_sample(ordered[0].trace),
    )


def _skill(lesson: Lesson) -> tuple[research.ResearchProposal, str]:
    """A candidata destilada do grupo mais forte, no formato do `research`.

    O slug carrega kind e as primeiras palavras da assinatura para o humano
    reconhecer a skill na listagem; `slugify` é do research de propósito — duas
    rotinas de slug é como os dois formatos de skill divergiriam.
    """
    head = " ".join(lesson.signature.split()[:3])
    topic = f"falha recorrente {head} em unidades {lesson.kind}".strip()
    slug = research.slugify(f"dream {lesson.kind} {head}")
    proposal = research.ResearchProposal(
        topic=topic,
        kind=lesson.kind,
        slug=slug,
        target_file=f"{research.SKILLS_SUBDIR}/{slug}.md",
        reasons=(
            f"dream:recurrent({lesson.n})",
            f"signature:{lesson.signature}",
            f"episodes:{','.join(str(i) for i in lesson.episode_ids)}",
        ),
    )
    return proposal, _skill_body(lesson)


def _skill_body(lesson: Lesson) -> str:
    """Corpo honesto: o que o banco diz, e que o valor disto é não-provado.

    Skill que se anuncia como verdade destilada de um LLM que nunca rodou
    enganaria o próximo run; aqui a evidência é a contagem, e a contagem está
    escrita no corpo.
    """
    units = ", ".join(lesson.unit_ids[:5]) or "(sem unit_id)"
    return (
        f"Consolidado da memória episódica: {lesson.n} falhas de unidades "
        f"kind={lesson.kind} com a mesma assinatura de trace.\n\n"
        f"## Assinatura recorrente\n\n`{lesson.signature}`\n\n"
        f"Unidades afetadas: {units}.\n\n"
        f"## Trecho representativo\n\n```\n{lesson.sample}\n```\n\n"
        "## O que fazer\n\n"
        f"- Antes de declarar pronta uma unidade kind={lesson.kind}, verifique "
        "explicitamente a condição que produz a falha acima — ela já reincidiu "
        f"{lesson.n} vezes.\n"
        "- Se o trecho aponta um comando de verificação, rode-o e leia a saída "
        "real; não presuma o resultado.\n"
        "- Se a causa for outra desta vez, diga qual: assinatura repetida com "
        "causa diferente é sinal de que esta orientação está errada.\n\n"
        "> Candidata do sono, não conhecimento validado: a evidência é a "
        "contagem de episódios, e o lift ainda vai ser medido pelo ciclo."
    )


def render_report(
    at: datetime,
    scanned: int,
    window_days: int,
    lessons: Sequence[Lesson] = (),
    orphans: Sequence[Orphan] = (),
    skill: research.ResearchProposal | None = None,
) -> str:
    """O paper trail. Markdown para o humano auditar o que o sono decidiu —
    contagens primeiro, evidência depois, cada lição com os ids que a
    sustentam."""
    lines = [
        f"# Dream {at.isoformat(timespec='seconds')}",
        "",
        "Consolidação offline da memória episódica (sem LLM).",
        "",
        f"- Episódios lidos: {scanned} (janela: {window_days}d)",
        f"- Lições fundidas: {len(lessons)}",
        f"- Órfãos arquivados: {len(orphans)}",
        f"- Skill candidata: {skill.target_file if skill else '(nenhuma)'}",
        "",
    ]
    if lessons:
        lines += ["## Lições fundidas", ""]
        for le in lessons:
            lines += [
                f"### {le.kind} — {le.n} episódios",
                "",
                f"- Assinatura: `{le.signature}`",
                f"- Episódios: {', '.join(f'#{i}' for i in le.episode_ids)}",
                f"- Unidades: {', '.join(le.unit_ids[:5]) or '(sem unit_id)'}",
                "",
                "```",
                le.sample,
                "```",
                "",
            ]
    if orphans:
        lines += ["## Órfãos arquivados (soft, nada foi deletado)", ""]
        for o in orphans:
            lines.append(f"- #{o.id} {o.kind}/{o.unit_id or '?'} ({o.created_at}): {o.sample}")
        lines.append("")
    if skill:
        lines += [
            "## Skill candidata",
            "",
            f"`{skill.target_file}` — destilada do grupo mais forte. Entra no ciclo",
            "normal de attribution/lift/prune; o sono propõe, o placar decide.",
            "",
        ]
    return "\n".join(lines)


def apply_dream(
    proposal: DreamProposal,
    root: Path | str | None = None,
    genome: Genome | None = None,
    data_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    now: datetime | str | None = None,
) -> DreamRecord:
    """Valida tudo, escreve a skill, arquiva os órfãos e grava o relatório.

    Nessa ordem e por LOTE: o genoma vem antes de qualquer escrita (mesmo
    fail-closed do `mutate.apply`, mesma exceção, para o chamador tratar
    REJECTED por um caminho só) e o arquivamento vem DEPOIS da skill — o
    arquivamento é a única parte que muda o que o `recall` devolve, e perder
    memória por causa de uma skill que não passou é o pior resultado possível.
    """
    if not proposal.lessons and not proposal.orphans:
        raise DreamError("sonho vazio: nada para consolidar")
    if not proposal.dream_report.strip():
        raise DreamError("sonho sem relatório: consolidação não auditável")

    base = root_dir(root)
    skill_text = ""
    if proposal.skill is not None:
        violations = mutate.check(proposal.skill, root=base, genome=genome)
        if violations:
            raise mutate.GenomeViolation(violations)
        skill_text = research.render_skill(proposal.skill, proposal.skill_body)
        # cinto e suspensório do research: skill ilegível não chega ao disco.
        research._parse_skill(skill_text)

    at = _clock(now)
    if skill_text:
        research._write_new(base / proposal.skill.target_file, skill_text)

    archived = episodic.archive(
        (o.id for o in proposal.orphans),
        db_path=Path(db_path) if db_path is not None else None,
    )
    report_path = dreams_dir(data_dir) / f"{_stamp(at)}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(proposal.dream_report, encoding="utf-8")
    return DreamRecord(
        report_path=report_path.as_posix(),
        skill_path=proposal.skill.target_file if proposal.skill else "",
        archived=archived,
        lessons=len(proposal.lessons),
        written_at=at.isoformat(timespec="seconds"),
    )


def dreams_dir(data_dir: Path | str | None = None) -> Path:
    """`<data_dir>/dreams`. O default é o data dir GLOBAL, o mesmo namespace da
    memória episódica — o relatório tem que ficar do lado do banco que ele
    descreve, e é por ele que o gatilho descobre quando foi o último sono."""
    base = Path(data_dir) if data_dir is not None else store.data_dir()
    return base / DREAMS_SUBDIR


def _stamp(at: datetime) -> str:
    """Nome de arquivo ordenável e sem `:` — o `now_iso` tem offset com dois
    pontos, que é legal em POSIX e péssimo em qualquer ferramenta que faça
    split por `:`."""
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def action() -> Action:
    """A ação registrável — consultada por `target.actions()` quando o wiring
    do registry chegar."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_dream, apply=apply_dream)
