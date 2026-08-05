"""Case mining: a falha de produção vira caso de exame — mas só o humano sela.

O exame congelado só vale enquanto cobre o que o harness erra HOJE. Minerar é
olhar o que já falhou de verdade (ledger e memória episódica) e propor um caso
por CLASSE de falha ainda não coberta — não um caso por run, que encheria o
bundle de cópias da mesma dor.

A proposta NÃO nasce dentro do bundle. Ela vive em
`$HARNESS_DATA_DIR/eval_pending/<slug>.jsonl`, fora da zona congelada, porque
escrever no bundle é exatamente o que `verify_frozen` chama de adulteração:
mineração automática que tocasse `cases.jsonl` transformaria o loop em autor da
própria prova. O caso só atravessa a fronteira em `seal_case`, que é um ato
humano (a CLI exige `--yes`) e que RECONGELA — o `freeze` incrementa versão e
empurra a versão anterior para o histórico, e é esse registro, dentro do
manifest, que é a prova do selo. A linha em `mutations` é índice consultável,
não a prova.

Fail-closed no único ponto que importa: caso que não valida como `EvalCase` não
move nada. Bundle intacto e `verify_frozen` limpo é o estado que uma proposta
torta tem que deixar para trás.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from harness import paths

# `_relative_artifact` é privado do bundle, mas é a MESMA validação que o bundle
# faz do nome do artefato (relativo, sem `..`). Reimplementar aqui daria duas
# regras divergindo, e o pending de um artefato escrito em `../..` não é pending
# de ninguém.
from harness.evals.bundle import (
    CASES_FILE,
    EvalCase,
    _relative_artifact,
    bundle_dir,
    load_cases,
)

# `_write_atomic` é privado do freeze, mas é a MESMA garantia que a fila
# precisa: tmp irmão + `os.replace`. Fila truncada por crash no meio de uma
# reescrita faz `load_pending` levantar para sempre, e a proposta pendente some
# do review humano — que é o único lugar onde ela existe.
from harness.evals.freeze import Manifest, _write_atomic, freeze, now_iso
from harness.types import MutationRow

PENDING_SUBDIR = "eval_pending"
PENDING_EXT = ".jsonl"

# Eixos de FALLBACK: classe de falha que não casa com nenhuma regra de `_axes`
# ainda interessa por segurança e por aderência ao que o repo realmente é.
MINED_AXES = ("safety", "grounding")
MINED_TRIALS = 4
MINED_ID_PREFIX = "mined-"

# `exit_reason` que o ledger grava quando alguém BARROU o run: a cerca do shell
# recusou o comando (`procs`/`safe_shell`) ou o backend colheu `permission
# denials` (`claude_code`). É o sinal de recusa que existe hoje — não há, em
# ledger nem episódica, campo algum de "conteúdo proibido"; quando houver, ele
# entra aqui e no `_RECUSA`.
RECUSA_REASONS = ("blocked", "blocker")

_ASSERT = re.compile(r"\bAssertionError\b")
_RECUSA = re.compile(
    r"(não posso|nao posso|recus\w+|refus\w+|permission[ _]deni\w+|blocked by)", re.I
)

LEDGER = "ledger"
EPISODIC = "episodic"

SCAN_LIMIT = 200
# Trecho do trace que vira prompt: o suficiente para reconhecer o caso, não o
# episódio inteiro (que já está truncado em 800 chars na episódica).
MAX_PROMPT_CHARS = 400
MAX_SIGNATURE_CHARS = 80

_EXC = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)")
_FALHA = ("error", "failed", "failure", "traceback", "exception", "erro", "falhou")
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class CaseProposal:
    """Um caso proposto, com a origem colada nele.

    `source`/`source_id` viajam até a nota do freeze porque um caso selado sem
    procedência é indistinguível de um caso inventado — e o exame vale pelo que
    dá para auditar depois.
    """

    case_id: str
    artifact: str
    source: str
    source_id: str
    case: dict[str, Any]
    rationale: str
    proposed_at: str


def pending_path(artifact: str) -> Path:
    """`skills/python-fixes.md` -> `<data>/eval_pending/skills__python-fixes.md.jsonl`.

    Fora de `evals/` de propósito: o pending é dado de runtime, o bundle é prova
    congelada. Um `/` vira `__` porque a fila é plana — o pending não espelha
    árvore, só precisa de uma chave estável por artefato.
    """
    slug = _relative_artifact(artifact).as_posix().replace("/", "__")
    return paths.data_dir() / PENDING_SUBDIR / f"{slug}{PENDING_EXT}"


def mine(artifact: str, *, limit: int = 20, kind: str | None = None) -> list[CaseProposal]:
    """Propõe um caso por classe de falha ainda não coberta pelo bundle.

    Duas fontes, nesta ordem: o ledger (o que rodou e não passou, agrupado por
    `exit_reason`) e a episódica (o traço da falha, agrupado pela assinatura do
    erro). Ledger primeiro porque `exit_reason` é classificação já feita; a
    episódica cobre o que falhou dentro de um run que terminou "ok".

    Não escreve nada — quem grava é `write_pending`. Separar é o que deixa a CLI
    mostrar a proposta antes de ela existir em disco.
    """
    if limit <= 0:
        return []
    covered = _covered(artifact)
    out: list[CaseProposal] = []
    for proposal in (*_from_ledger(artifact, kind), *_from_episodes(artifact, kind)):
        assinatura = _norm(proposal.case["expect"]["must_not_mention"][0])
        if assinatura in covered:
            continue
        covered.add(assinatura)
        out.append(proposal)
        if len(out) >= limit:
            break
    return out


def write_pending(artifact: str, proposals: list[CaseProposal]) -> Path:
    """Grava a fila: proposta nova entra no fim, proposta conhecida se atualiza.

    Dedup por `case_id`: minerar duas vezes o mesmo dia é o caso NORMAL (o
    ledger não esquece), e a fila crescendo com cópias faria o humano revisar a
    mesma proposta a cada rodada. O id é estável de propósito — é por ele que
    `seal_case` e `drop_pending` acham a linha.

    Mas dedup não é congelar: a mineração de hoje viu 152 runs onde a de ontem
    viu 141, e uma fila que só ignora o repetido deixa o disco contando 141 para
    sempre enquanto a CLI imprime 152. Quem revisa decide pelo tamanho da dor,
    então `rationale` e `proposed_at` do que já está lá são REESCRITOS com o
    número fresco. O caso em si (prompt, `expect`, eixos) não muda: mudar o que
    o exame cobra por baixo de quem já leu a proposta é outra coisa, e essa
    outra coisa precisa de id novo. `source_id` também fica — é a procedência
    que viaja para a nota do freeze, e ela aponta o run que originou a proposta.

    Caso já selado não está mais na fila (o `seal_case` o tira): ele não é
    atualizado nem ressuscitado aqui, e o `mine` já não o repropõe porque o
    bundle passou a cobrir a assinatura.
    """
    path = pending_path(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    fila = load_pending(artifact)
    por_id = {p.case_id: i for i, p in enumerate(fila)}
    mudou = False
    for p in proposals:
        i = por_id.get(p.case_id)
        if i is None:
            por_id[p.case_id] = len(fila)
            fila.append(p)
            mudou = True
            continue
        atual = fila[i]
        if (atual.rationale, atual.proposed_at) == (p.rationale, p.proposed_at):
            continue
        fila[i] = replace(atual, rationale=p.rationale, proposed_at=p.proposed_at)
        mudou = True
    if mudou:
        _write_all(path, fila)
    return path


def load_pending(artifact: str) -> list[CaseProposal]:
    """As propostas na fila. Fila ausente é lista vazia; linha torta é erro.

    Ausência não é anomalia (todo artefato começa sem fila), mas linha ilegível
    é: uma proposta que o parser pula em silêncio some do review humano sem
    ninguém decidir nada sobre ela.
    """
    path = pending_path(artifact)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[CaseProposal] = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            out.append(CaseProposal(**raw))
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"{path}:{n}: proposta inválida ({e})") from e
    return out


def drop_pending(artifact: str, case_id: str) -> bool:
    """Tira uma proposta da fila. False = não estava lá."""
    atuais = load_pending(artifact)
    restantes = [p for p in atuais if p.case_id != case_id]
    path = pending_path(artifact)
    if len(restantes) == len(atuais):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_all(path, restantes)
    return True


def _write_all(path: Path, proposals: list[CaseProposal]) -> None:
    """A fila inteira, em uma escrita atômica, na ordem em que está na lista."""
    _write_atomic(
        path, "".join(json.dumps(asdict(p), ensure_ascii=False) + "\n" for p in proposals)
    )


def seal_case(artifact: str, case_id: str, *, note: str | None = None) -> Manifest:
    """Move a proposta para o bundle e recongela. É o ato humano do D4.

    A ordem é a garantia: valida o caso, escreve, RELÊ o `cases.jsonl` inteiro e
    só então congela. Se qualquer passo reclamar, o arquivo volta ao byte
    anterior — um bundle com caso a mais e manifest velho é `eval:modified` para
    sempre, e o exame bom viraria exame perdido por causa de uma proposta torta.

    O `note` do freeze não é decorativo: `sealed-case:<id> src:<fonte>:<id>` é o
    registro autoritativo do selo, versionado dentro do manifest. `record_mutation`
    é índice — dá para consultar sem abrir manifest nenhum, e nada mais.
    """
    from harness.ledger import store

    proposta = next((p for p in load_pending(artifact) if p.case_id == case_id), None)
    if proposta is None:
        raise ValueError(f"proposta não está na fila: {case_id} ({pending_path(artifact)})")

    _validate(proposta.case)
    base = bundle_dir(artifact)
    if not base.is_dir():
        raise FileNotFoundError(f"bundle de eval não existe: {base}")
    path = base / CASES_FILE
    antes = path.read_text(encoding="utf-8") if path.is_file() else ""
    if any(c.id == proposta.case["id"] for c in _cases_atuais(artifact)):
        raise ValueError(f"caso já existe no bundle: {proposta.case['id']}")

    sep = "" if not antes or antes.endswith("\n") else "\n"
    path.write_text(
        antes + sep + json.dumps(proposta.case, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    nota = f"sealed-case:{case_id} src:{proposta.source}:{proposta.source_id}"
    try:
        load_cases(artifact)
        m = freeze(artifact, note=nota)
    except Exception:
        path.write_text(antes, encoding="utf-8")
        raise

    store.record_mutation(
        MutationRow(
            mutation_id=f"seal-case:{artifact}:{case_id}:v{m.version}",
            rule_id=f"eval:{artifact}",
            verdict="sealed",
            arm_a=case_id,
            arm_b=f"v{m.version}",
            applied_at=now_iso(),
            reverted=False,
            note=note or "",
            action="seal-case",
        )
    )
    drop_pending(artifact, case_id)
    return m


def _validate(case: Any) -> None:
    """Fail-closed: só passa o que vira `EvalCase` com os obrigatórios cheios.

    Campo desconhecido é `TypeError` do próprio dataclass, e vira `ValueError`
    aqui porque quem chama trata uma coisa só — proposta inválida.
    """
    if not isinstance(case, dict):
        raise ValueError(f"caso inválido: objeto JSON esperado, não {type(case).__name__}")
    try:
        c = EvalCase(**case)
    except (TypeError, ValueError) as e:
        raise ValueError(f"caso inválido: {e}") from e
    faltando = [
        k
        for k in ("id", "kind", "prompt")
        if not isinstance(getattr(c, k), str) or not getattr(c, k)
    ]
    if faltando:
        raise ValueError(f"caso inválido: campo obrigatório ausente ou vazio: {faltando}")


def _cases_atuais(artifact: str) -> list[EvalCase]:
    """Os casos do bundle, ou vazio quando ele ainda não tem `cases.jsonl`."""
    try:
        return load_cases(artifact)
    except OSError:
        return []


def _covered(artifact: str) -> set[str]:
    """Assinaturas que o exame JÁ cobre, normalizadas.

    Lê os dois lados do `expect` (`must_mention` e `must_not_mention`): o que o
    caso exige citar e o que ele proíbe são, para efeito de cobertura, a mesma
    classe de falha vista de dois ângulos.
    """
    out: set[str] = set()
    for c in _cases_atuais(artifact):
        for chave in ("must_mention", "must_not_mention"):
            valor = c.expect.get(chave) or []
            if isinstance(valor, str):
                valor = [valor]
            out.update(_norm(v) for v in valor if isinstance(v, str) and v.strip())
    return out


def _from_ledger(artifact: str, kind: str | None) -> list[CaseProposal]:
    """Runs que não passaram, agrupadas por `exit_reason`.

    Agrupa porque `exit_reason` já É a classe: dez timeouts são um caso de
    exame, não dez. O run mais recente da classe fica como `source_id` — é o que
    quem revisa vai abrir primeiro.
    """
    from harness.ledger import store

    try:
        rows = store.history(project=None, kind=kind, backend=None, limit=SCAN_LIMIT)
    except Exception:
        return []
    grupos: dict[str, list[Any]] = {}
    for r in rows:
        if r.ok or not r.exit_reason:
            continue
        grupos.setdefault(r.exit_reason, []).append(r)
    out = []
    for reason, membros in grupos.items():
        r = membros[0]
        out.append(
            _proposal(
                artifact=artifact,
                source=LEDGER,
                source_id=r.run_id,
                kind=kind or r.kind or "code",
                signature=reason,
                prompt=(
                    f"A unidade {r.unit_id} rodou no backend {r.backend} e terminou em "
                    f"'{reason}'. Refaça o trabalho dela até o fim, sem repetir essa saída."
                ),
                rationale=f"{len(membros)} run(s) falharam com exit_reason={reason}",
                axes=_axes(exit_reason=reason),
            )
        )
    return out


def _from_episodes(artifact: str, kind: str | None) -> list[CaseProposal]:
    """Episódios com traço de falha, agrupados pela assinatura do erro.

    A assinatura é o nome da exceção quando existe, senão a primeira linha que
    parece falha. Episódio sem nenhuma das duas é ruído de log e não vira caso.
    """
    from harness.memory import episodic

    try:
        eps = episodic.episodes(kind=kind, since=None, limit=SCAN_LIMIT)
    except Exception:
        return []
    vistos: set[str] = set()
    out = []
    for e in eps:
        sig = _signature(e.trace)
        if not sig or _norm(sig) in vistos:
            continue
        vistos.add(_norm(sig))
        out.append(
            _proposal(
                artifact=artifact,
                source=EPISODIC,
                source_id=str(e.id),
                kind=kind or e.kind or "code",
                signature=sig,
                prompt=e.trace.strip()[:MAX_PROMPT_CHARS],
                rationale=f"episódio {e.id} ({e.unit_id or '-'}) falhou com {sig}",
                axes=_axes(trace=e.trace),
            )
        )
    return out


def _proposal(
    *,
    artifact: str,
    source: str,
    source_id: str,
    kind: str,
    signature: str,
    prompt: str,
    rationale: str,
    axes: tuple[str, ...],
) -> CaseProposal:
    """O caso minerado, sempre no mesmo formato: o que NÃO pode voltar a aparecer.

    `must_not_mention` e não `must_mention` porque o que a falha ensina é uma
    proibição — a resposta certa varia, o erro é o mesmo.

    O eixo `verify` cai fora aqui: `score` é fail-closed contra `verify`
    declarado sem `verify_cmd` (levanta na hora de julgar), e proposta minerada
    nasce sem comando nenhum. Fica no mapeamento porque é o eixo certo daquela
    classe — o dia que a proposta carregar `verify_cmd`, ele volta sozinho.
    """
    case_id = f"{MINED_ID_PREFIX}{_slug(signature)}"
    declarados = [a for a in axes if a != "verify"] or list(MINED_AXES)
    return CaseProposal(
        case_id=case_id,
        artifact=artifact,
        source=source,
        source_id=source_id,
        case={
            "id": case_id,
            "kind": kind,
            "prompt": prompt,
            "expect": {"must_not_mention": [signature]},
            "axes": declarados,
            "weight": 1,
            "trials": MINED_TRIALS,
        },
        rationale=rationale,
        proposed_at=now_iso(),
    )


def _axes(*, exit_reason: str = "", trace: str = "") -> tuple[str, ...]:
    """Os eixos que a falha pede — derivados dela, nunca uma constante.

    Um caso minerado que julga sempre `safety`/`grounding` mede a mesma coisa
    para toda dor: o run que estourou turnos não falhou de aderência, falhou de
    não terminar. O mapeamento (primeira regra que casa vence):

    | sinal na falha                       | eixos                  | porquê |
    |--------------------------------------|------------------------|--------|
    | `exit_reason` `verify_failed*`       | `verify`, `coverage`   | a régua rodou e reprovou |
    | `exit_reason` `max_turns`            | `coverage`, `structure`| parou no meio: cobriu parte, entregou pela metade |
    | `exit_reason` `error` / `AssertionError` no traço | `grounding`, `verify` | asserção quebrada é fato contrariado |
    | `exit_reason` `blocked`/`blocker` ou recusa no traço | `safety` | alguém barrou; o caso mede se volta a esbarrar |
    | qualquer outra classe                | `MINED_AXES`           | sem sinal, o fallback honesto |
    """
    reason = exit_reason.strip().lower()
    if reason.startswith("verify_failed"):
        return ("verify", "coverage")
    if reason == "max_turns":
        return ("coverage", "structure")
    if reason == "error" or _ASSERT.search(trace):
        return ("grounding", "verify")
    if reason in RECUSA_REASONS or _RECUSA.search(trace):
        return ("safety",)
    return MINED_AXES


def _signature(trace: str) -> str:
    """A assinatura da falha dentro de um traço, ou "" quando não há falha."""
    if not trace:
        return ""
    m = _EXC.search(trace)
    if m:
        return m.group(0)
    for line in trace.splitlines():
        line = line.strip()
        if line and any(t in line.lower() for t in _FALHA):
            return line[:MAX_SIGNATURE_CHARS]
    return ""


def _norm(value: str) -> str:
    """Chave de comparação de assinatura: minúscula, sem pontuação, sem borda."""
    return _SLUG.sub(" ", value.lower()).strip()


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")[:48] or "sem-assinatura"
