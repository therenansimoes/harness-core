"""Ação 'tune': afina um artefato contra o exame congelado dele.

O ciclo é o mesmo de sempre — propor, medir, comparar, reter —, mas com a única
garantia que faltava: o exame não se mexe. `verify_frozen` roda ANTES de
qualquer pontuação, e bundle adulterado aborta sem medir nada. Nota tirada
contra um exame que o próprio tuner poderia ter editado não é nota, é opinião
com casas decimais.

Três coisas separam isto de "peça ao modelo para melhorar a skill":

1. BASELINE TRIPLO. `none` (sem artefato nenhum), `draft` (o que está no disco)
   e `tuned` (o que saiu do loop). Sem o `none` ninguém sabe se a skill ajuda ou
   se o modelo já resolvia sozinho — e essa é a pergunta cara.
2. GATE MONOTÔNICO. Uma candidata só é RETIDA se bater a melhor no overall E
   não regredir no holdout; candidata descartada NÃO encerra a cadeia —
   `rounds` é o número de TENTATIVAS de reescrita, e o vencedor é sempre a
   última retida. O motivo do descarte vira feedback da próxima tentativa.
3. VALIDADE ANTES DE NOTA. Versão que não passa no `validate` do adapter é
   descartada SEM ser pontuada. Pontuar artefato inválido gastaria backend para
   medir algo que nunca poderia ser gravado.

A cadeia inteira é gravada em `$HARNESS_DATA_DIR/tune/<artefato>/`, NUNCA dentro
do bundle: arquivo novo no bundle é `eval:unlisted-file`, e o loop derrubaria o
próprio exame ao escrever o relatório dele.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from harness import paths
from harness.backends.registry import get_backend
from harness.evals.bundle import EvalCase, load_cases, split_cases
from harness.evals.freeze import verify_frozen
from harness.evals.report import render_evaluation_md
from harness.evals.score import (
    RULER_VERSION,
    Aggregate,
    TrialResult,
    aggregate,
    beats,
    score_trial,
)
from harness.improve import mutate, research, workflow_action
from harness.improve.tunable import Tunable, tunable_for
from harness.ledger import store
from harness.types import ExecRequest, MutationRow

ACTION = "tune"
TUNE_SUBDIR = "tune"
CHAIN_FILE = "chain.json"
EVALUATION_FILE = "EVALUATION.md"
TRIALS_SUFFIX = ".trials.jsonl"
# Teto do texto julgado gravado por trial: o arquivo é evidência para leitura,
# não cópia do artefato — quem quer o texto inteiro lê o `v{n}.txt` ao lado.
TRIAL_OUTPUT_MAX = 4000

# Mock por default pelo mesmo motivo do resto do pacote: o caminho feliz de
# quem só quer ver o loop rodar não pode exigir chave de API.
TUNE_BACKEND = "mock"
TUNE_TIMEOUT_S = 600.0
DEFAULT_MODEL = "openai:qwen/qwen3.5-9b"
DEFAULT_MAX_USD = 0.25
# 2 tentativas de reescrita = até v3. Mais que isso raramente muda o vencedor e
# sempre muda a fatura; quem quiser cadeia longa passa `rounds`.
DEFAULT_ROUNDS = 2

# O rewriter é a única parte do loop que ganha modelo de verdade: propor texto
# novo é o que o mock não sabe fazer (ele devolve o próprio prompt). O runner
# fica onde está de propósito — juiz que muda entre versões não compara nada.
REWRITE_BACKEND = "deepagents"
REWRITE_MODEL = DEFAULT_MODEL
# Reescrever artefato inteiro em modelo pequeno local é lento; o teto do runner
# (600s) seria tempo demais para descobrir que o servidor travou.
REWRITE_TIMEOUT_S = 180.0
# O trace do deepagents corta conteúdo em 4000 chars e não tem chave `result`:
# artefato reescrito volta por ARQUIVO ou não volta inteiro.
REWRITE_FILE = "rewrite.out"
# Escrever arquivo custa no MÍNIMO dois turnos (a tool call e o fecho depois do
# resultado); com 1 o `ModelCallLimitMiddleware` corta no meio e todo run real
# viraria fallback para o mock.
REWRITE_MAX_TURNS = 4
# Guarda dura de máquina, não de custo: 30B local trava o note inteiro. Modelo
# grande pedido na linha de comando cai para o REWRITE_MODEL em silêncio.
BANNED_LOCAL = ("30b", "32b", "27b")
# Contradiz de propósito o "responda APENAS com o artefato" do `rewrite_prompt`:
# o 9B local, deixado por conta dele, responde no chat e a resposta do chat volta
# cortada. A ordem vem DEPOIS no prompt para ser a última coisa que ele lê.
REWRITE_ORDER = (
    "ATENÇÃO — isto muda a instrução acima: NÃO responda com o artefato no "
    f"chat. Chame a tool `write_file` com `file_path` = `{REWRITE_FILE}` e o "
    "artefato COMPLETO reescrito em `content` (sem cercas de código, sem "
    "comentário em volta). Só depois responda uma linha dizendo que gravou. "
    f"O que vai ser medido é o conteúdo de `{REWRITE_FILE}`; texto solto no "
    "chat é descartado."
)

PROMOTED = "promoted"
HELD = "held"

_EMPTY = Aggregate(per_axis={}, lower={}, overall=0.0, n=0)


class TuneAborted(Exception):
    """Exame adulterado: nada foi medido e nada será gravado."""


class TuneError(Exception):
    """Backend falhou, estourou o teto ou devolveu texto vazio."""


@dataclass(frozen=True)
class TuneVersion:
    """Um elo da cadeia.

    `valid` significa SÓ "passou no `validate` do adapter"; `retained` significa
    "entrou na cadeia monotônica". Candidata pontuada mas descartada no gate é
    `valid=True, retained=False` — a conflação antiga (descartada = inválida)
    acabou aqui. `valid=False` => `agg` é o placar vazio, não um zero medido: a
    versão nem chegou a ser pontuada.

    `agg` é o bundle INTEIRO (a manchete do EVALUATION.md), `train` é o mesmo
    placar restrito ao treino — e é o `train` que o gate compara. O reescritor
    só enxerga treino; julgá-lo por uma média que inclui o holdout seria cobrar
    dele o que ele não teve como ver, e é o que travava a cadeia.
    """

    version: int
    text: str
    agg: Aggregate
    reason: str
    valid: bool = True
    retained: bool = True
    holdout: Aggregate = _EMPTY
    train: Aggregate = _EMPTY
    trials: tuple[TrialResult, ...] = field(default=(), repr=False)
    outputs: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class TuneOutcome:
    """O resultado do loop. `winner` é o índice na cadeia (1-based)."""

    artifact: str
    baseline: dict[str, Aggregate]
    chain: list[TuneVersion]
    winner: int
    evaluation_md: str

    @property
    def winning(self) -> TuneVersion:
        return self.chain[self.winner - 1]


@dataclass(frozen=True)
class TuneProposal:
    """O que o `apply` vai gravar. `target_file` é o que o genoma julga —
    mesmo contrato duck-typed do `mutate.check`.

    Carrega o `outcome` inteiro (e não só o texto) porque a linha de mutação
    precisa do motivo e do par de braços: registrar "promovido" sem o placar que
    justificou seria um veredito sem prova. `inner` é a proposta do adapter
    (`WorkflowProposal`/`ResearchProposal`), guardada para o apply revalidar pelo
    mesmo caminho das outras ações.
    """

    artifact: str
    target_file: str
    text: str
    outcome: TuneOutcome
    inner: object


@dataclass(frozen=True)
class TuneRecord:
    """O que ficou no disco. Só tipos inertes: atravessa checkpoint como dict."""

    artifact: str
    verdict: str
    winner: int
    reason: str
    written_path: str | None
    chain_dir: str
    recorded_at: str


def run_tune(
    artifact: str,
    *,
    candidate: str | None = None,
    rounds: int = DEFAULT_ROUNDS,
    trials: int | None = None,
    model: str = DEFAULT_MODEL,
    max_usd: float = DEFAULT_MAX_USD,
) -> TuneOutcome:
    """Afina `artifact` contra o bundle congelado dele e devolve a cadeia.

    Não escreve o artefato: quem grava é o `apply_tune`, depois do genoma. Aqui
    só se mede e se registra a cadeia em `data/`.
    """
    violations = verify_frozen(artifact)
    if violations:
        raise TuneAborted(violations)

    adapter = tunable_for(artifact, model=model, max_usd=max_usd)
    cases = load_cases(artifact)
    train_cases, hold_cases = split_cases(cases)
    train_ids = {c.id for c in train_cases}
    hold_ids = {c.id for c in hold_cases}
    weights = {c.id: c.weight for c in cases}

    # Controle: o que sai SEM artefato nenhum. Passa por fora do `validate` de
    # propósito — texto vazio nunca é uma skill válida, e barrá-lo aqui mataria
    # justamente a medição que diz se o artefato paga o próprio custo.
    none_agg, _, _ = _score(adapter, cases, "", trials, weights)

    # v1 é o incumbente (é o que já está no disco), não uma candidata: o gate
    # julga de v2 em diante. v1 inválida encerra antes do loop — não há nada
    # medido para reescrever.
    text = candidate if candidate is not None else adapter.read()
    errs = adapter.validate(text)
    best: TuneVersion | None = None
    if errs:
        chain = [
            TuneVersion(1, text, _EMPTY, f"v1 descartada: {errs[0]}", valid=False, retained=False)
        ]
    else:
        v1 = _measured(1, text, adapter, cases, trials, weights, hold_ids, train_ids)
        best = replace(v1, retained=True, reason=_reason(1, v1.agg, none_agg))
        chain = [best]

    # `rounds` = número de TENTATIVAS de reescrita: candidata descartada não
    # encerra a cadeia — o motivo do descarte vira feedback da próxima.
    feedback: list[str] = []
    if best is not None:
        for n in range(2, rounds + 2):
            # Só treino nos três argumentos: casos, reprovações e placar. Mostrar
            # o holdout ao reescritor transformaria a prova de generalização em
            # mais um alvo de otimização.
            prompt = _with_feedback(
                adapter.rewrite_prompt(
                    best.text,
                    _weak(best.trials, train_ids),
                    cases=train_cases,
                    agg=best.train,
                ),
                feedback,
            )
            cand = _call_rewriter(prompt, model=model, max_usd=max_usd)
            errs = adapter.validate(cand)
            if errs:
                reason = f"v{n} descartada: {errs[0]}"
                chain.append(TuneVersion(n, cand, _EMPTY, reason, valid=False, retained=False))
                feedback.append(reason)
                continue
            v = _measured(n, cand, adapter, cases, trials, weights, hold_ids, train_ids)
            # Treino decide, holdout veta: é o par que o reescritor pode
            # atacar e a parte que ele não pode ver, nessa ordem.
            if beats(v.train, best.train) and _holdout_ok(v, best):
                v = replace(v, retained=True, reason=_reason(n, v.agg, best.agg))
                chain.append(v)
                best = v
                feedback = []  # base nova -> feedback antigo expira
            else:
                reason = _discard_reason(n, v, best)
                chain.append(replace(v, retained=False, reason=reason))
                feedback.append(reason)

    winner = best.version if best is not None else 1
    win = chain[winner - 1]
    baseline = {
        "none": none_agg,
        "draft": chain[0].agg,
        "tuned": best.agg if best is not None else _EMPTY,
    }
    md = render_evaluation_md(artifact, scores=win.agg, baseline=baseline)
    _persist(artifact, chain, md)
    return TuneOutcome(
        artifact=artifact, baseline=baseline, chain=chain, winner=winner, evaluation_md=md
    )


def propose_tune(
    artifact: str,
    *,
    candidate: str | None = None,
    rounds: int = DEFAULT_ROUNDS,
    trials: int | None = None,
    model: str = DEFAULT_MODEL,
    max_usd: float = DEFAULT_MAX_USD,
) -> TuneProposal:
    """Roda o loop e embrulha o vencedor na proposta do adapter.

    A medição inteira acontece no propose porque é ela que DECIDE o que propor —
    um `propose` barato aqui proporia texto que ninguém pontuou.
    """
    outcome = run_tune(
        artifact, candidate=candidate, rounds=rounds, trials=trials, model=model, max_usd=max_usd
    )
    win = outcome.winning
    return TuneProposal(
        artifact=artifact,
        target_file=artifact,
        text=win.text,
        outcome=outcome,
        inner=_inner_proposal(artifact, win, outcome),
    )


def apply_tune(
    proposal: TuneProposal,
    root: Path | str | None = None,
    genome=None,
) -> TuneRecord:
    """Genoma ANTES da escrita, e escrita só se houve promoção.

    Cadeia que parou em v1 é resultado legítimo ("o que está no disco continua
    sendo o melhor") e mesmo assim grava a linha de mutação: experimento sem
    veredito registrado é experimento que alguém vai repetir amanhã.
    """
    violations = mutate.check(proposal, root=root, genome=genome)
    if violations:
        raise mutate.GenomeViolation(violations)

    outcome = proposal.outcome
    win = outcome.winning
    promoted = outcome.winner > 1 and win.valid
    written: Path | None = None
    if promoted:
        adapter = tunable_for(proposal.artifact, root=root)
        written = adapter.write(win.text, win.version)

    ts = store.now_iso()
    rule_id = f"{ACTION}:{proposal.artifact}"
    store.record_mutation(
        MutationRow(
            mutation_id=f"{rule_id}@{ts}",
            rule_id=rule_id,
            verdict=PROMOTED if promoted else HELD,
            arm_a=f"v{outcome.winner - 1}",
            arm_b=f"v{outcome.winner}",
            applied_at=ts,
            reverted=False,
            note=win.reason,
            action=ACTION,
        )
    )
    return TuneRecord(
        artifact=proposal.artifact,
        verdict=PROMOTED if promoted else HELD,
        winner=outcome.winner,
        reason=win.reason,
        written_path=str(written) if written is not None else None,
        chain_dir=str(chain_dir(proposal.artifact)),
        recorded_at=ts,
    )


def chain_dir(artifact: str) -> Path:
    """`skills/python-fixes.md` -> `<data>/tune/skills/python-fixes`.

    Espelha o path como o `bundle_dir` faz, em vez de usar só o slug: dois
    artefatos de mesmo nome em zonas diferentes escreveriam a cadeia um do
    outro, e a cadeia é a única evidência de por que a versão ganhou.
    """
    return paths.data_dir() / TUNE_SUBDIR / PurePosixPath(Path(artifact).as_posix()).with_suffix("")


def _run_case(text: str, case: EvalCase, *, model: str | None, max_usd: float) -> str:
    """Medição extrativa determinística — a saída julgada É o artefato.

    Runner real (LM Studio por caso) deliberadamente ADIADO: variância de runner
    dentro do juiz mata a comparabilidade entre versões. Os params `model` e
    `max_usd` ficam pela assinatura do futuro runner real e pelo seam de teste.
    """
    return text


def _call_runner(prompt: str, *, model: str | None, max_usd: float) -> str:
    """DEPRECIADO: seam antigo do runner, morto desde a medição extrativa.

    Mantido só porque `tests/test_rollback.py` ainda troca este atributo via
    monkeypatch (arquivo fora do escopo desta frente); remover junto com aquele
    fixture.
    """
    return _call(prompt, model=model, max_usd=max_usd, purpose="run")


def _call_rewriter(prompt: str, *, model: str | None, max_usd: float) -> str:
    """UMA chamada ao backend para reescrever o artefato. Seam de teste.

    Tenta o modelo local; se o LM Studio não estiver de pé, ou se ele estiver e
    a chamada falhar, cai no mock. Fallback importa mais aqui do que qualidade:
    a cadeia é monotônica, então proposta ruim (ou a devolução crua do mock)
    perde no gate e o vencedor continua sendo o mesmo de antes.
    """
    backend, m = _rewrite_target(model)
    if backend == TUNE_BACKEND:
        return _call(prompt, model=m, max_usd=max_usd, purpose="rewrite")
    try:
        return _call(
            f"{prompt}\n\n{REWRITE_ORDER}",
            model=m,
            max_usd=max_usd,
            purpose="rewrite",
            backend=backend,
            tools=("read_file", "write_file"),
            max_turns=REWRITE_MAX_TURNS,
            timeout_s=REWRITE_TIMEOUT_S,
            out_file=REWRITE_FILE,
        )
    except Exception:
        return _call(prompt, model=model, max_usd=max_usd, purpose="rewrite")


def _rewrite_target(model: str | None) -> tuple[str, str | None]:
    """Qual backend (e com qual modelo) vai reescrever. Nunca levanta.

    A sonda é o preflight do deepagents — `GET /v1/models`, zero token —, então
    perguntar "dá para usar o modelo de verdade?" é de graça. Import lá dentro
    porque o backend é opcional: quem instalou só o core não paga por ele.
    """
    m = model if model and model.startswith("openai:") else REWRITE_MODEL
    if any(big in m.lower() for big in BANNED_LOCAL):
        m = REWRITE_MODEL
    try:
        from harness.backends.deepagents_backend import DeepagentsBackend

        if DeepagentsBackend(model=m).preflight().ok:
            return REWRITE_BACKEND, m
    except Exception:
        pass
    return TUNE_BACKEND, model


def _call(
    prompt: str,
    *,
    model: str | None,
    max_usd: float,
    purpose: str,
    backend: str = TUNE_BACKEND,
    tools: tuple[str, ...] = ("Read",),
    max_turns: int = 1,
    timeout_s: float = TUNE_TIMEOUT_S,
    out_file: str | None = None,
) -> str:
    """O padrão do `add._call_author`: workspace efêmero, teto de custo, texto
    ou erro — nunca string vazia passando por resposta."""
    b = get_backend(backend)
    pre = b.preflight()
    if not pre.ok:
        raise TuneError(f"backend {backend} indisponível: {pre.reason}")
    ws = Path(tempfile.mkdtemp(prefix=f"harness-tune-{purpose}-"))
    trace = ws / "trace.jsonl"
    # O workspace tem que existir ANTES: agente que recebe ordem de escrever
    # arquivo num diretório inexistente falha na primeira tool call.
    (ws / "out").mkdir(parents=True, exist_ok=True)
    try:
        result = b.execute(
            ExecRequest(
                prompt=prompt,
                workspace=ws / "out",
                tools=tools,
                model=model,
                max_turns=max_turns,
                timeout_s=timeout_s,
                trace_path=trace,
            )
        )
        if not result.ok:
            raise TuneError(f"chamada de {purpose} falhou: exit_reason={result.exit_reason}")
        if result.cost_usd is not None and result.cost_usd > max_usd:
            raise TuneError(
                f"{purpose} custou ${result.cost_usd:.4f} > teto ${max_usd:.2f} — cadeia parada"
            )
        text = (
            _out_file_text(ws / "out", out_file)
            or _result_text(trace)
            or _workspace_text(ws / "out", result.files_changed)
        )
        if not text:
            raise TuneError(f"backend {backend!r} não devolveu texto de {purpose}")
        return text
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _out_file_text(ws: Path, out_file: str | None) -> str:
    """O arquivo combinado, quando houve um. Vem antes do trace porque o trace do
    deepagents corta conteúdo: artefato cortado passaria por artefato inteiro."""
    if not out_file:
        return ""
    p = ws / out_file
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


def _result_text(trace: Path) -> str:
    """O `--output-format json` do CLI põe a resposta em `result`."""
    try:
        lines = trace.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            return obj["result"]
    return ""


def _workspace_text(ws: Path, files_changed: Sequence[str]) -> str:
    """Fallback para backend que responde em ARQUIVO e não em trace (é o caso do
    mock, e do `research._distill`). Sem isto o default do pacote seria um
    backend que nunca devolve nada."""
    parts = []
    for name in files_changed:
        p = ws / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)


def _score(
    adapter: Tunable,
    cases: Sequence[EvalCase],
    text: str,
    trials: int | None,
    weights: dict[str, float],
) -> tuple[Aggregate, list[TrialResult], list[str]]:
    """Roda `trials` tentativas por caso e agrega. `trials=None` = o do caso.

    Devolve as SAÍDAS junto (alinhadas por índice com os trials) porque o
    `TrialResult` guarda o veredito e não o que foi julgado — e sem o texto
    julgado o `v{n}.trials.jsonl` seria diagnóstico sem corpo de delito.
    """
    results: list[TrialResult] = []
    outputs: list[str] = []
    for case in cases:
        n = trials if trials is not None else case.trials
        for i in range(max(1, n)):
            out = adapter.produce(case, text)
            results.append(score_trial(case, out, trial=i))
            outputs.append(out)
    return aggregate(results, weights), results, outputs


def _weak(results: Sequence[TrialResult], train_ids: set[str]) -> list[TrialResult]:
    """Só trials de TREINO com eixo reprovado — o reescritor nunca vê o holdout."""
    return [r for r in results if r.case_id in train_ids and not all(r.axes.values())]


def _measured(
    n: int,
    text: str,
    adapter: Tunable,
    cases: Sequence[EvalCase],
    trials: int | None,
    weights: dict[str, float],
    hold_ids: set[str],
    train_ids: set[str],
) -> TuneVersion:
    """Pontua uma versão válida no bundle inteiro + as visões treino e holdout.

    Três agregados sobre a MESMA medição: ninguém roda o exame duas vezes para
    ter as duas visões — elas são recortes da mesma lista de trials.
    """
    agg, results, outputs = _score(adapter, cases, text, trials, weights)
    hold_agg = (
        aggregate([r for r in results if r.case_id in hold_ids], weights) if hold_ids else _EMPTY
    )
    train_agg = aggregate([r for r in results if r.case_id in train_ids], weights)
    return TuneVersion(
        n,
        text,
        agg,
        reason="",
        valid=True,
        retained=False,
        holdout=hold_agg,
        train=train_agg,
        trials=tuple(results),
        outputs=tuple(outputs),
    )


def _holdout_ok(new: TuneVersion, old: TuneVersion) -> bool:
    """Gate de holdout é NÃO-REGRESSÃO (>=), não vitória estrita: com holdout de
    2 casos, exigir vitória lá travaria toda candidata que só melhora o treino."""
    return new.holdout.n == 0 or new.holdout.overall >= old.holdout.overall - 1e-9


def _discard_reason(n: int, v: TuneVersion, best: TuneVersion) -> str:
    """Por que a candidata caiu, COM o delta por eixo e o que fazer a seguir.

    "overall X <= Y" manda o reescritor tentar de novo às cegas; "PIOROU
    clarity 0.510->0.000, GANHOU coverage 0.000->0.097" diz que a rodada
    acertou o alvo e derrubou outra coisa no caminho — que é uma correção
    dirigida em uma linha, e não uma nova rodada de dados.
    """
    head = (
        f"v{n} descartada: train {v.train.overall:.3f} <= {best.train.overall:.3f}"
        if not beats(v.train, best.train)
        else f"v{n} descartada: holdout {v.holdout.overall:.3f} < {best.holdout.overall:.3f}"
    )
    piorou = _axis_deltas(v.train, best.train, worse=True)
    ganhou = _axis_deltas(v.train, best.train, worse=False)
    partes = [head]
    if piorou:
        partes.append("PIOROU " + ", ".join(piorou))
    if ganhou:
        partes.append("GANHOU " + ", ".join(ganhou))
    if piorou and ganhou:
        partes.append(f"preserve os ganhos e recupere {_axis_names(piorou)}")
    elif piorou:
        partes.append(f"recupere {_axis_names(piorou)}")
    else:
        partes.append("nenhum eixo regrediu; o ganho não bateu o incumbente")
    return "; ".join(partes) + "."


def _axis_deltas(new: Aggregate, old: Aggregate, *, worse: bool) -> list[str]:
    """`axis a->b` dos eixos que mudaram no sentido pedido, em ordem alfabética."""
    out = []
    for axis in sorted(set(new.lower) | set(old.lower)):
        a, b = old.lower.get(axis, 0.0), new.lower.get(axis, 0.0)
        if (b < a - 1e-9) if worse else (b > a + 1e-9):
            out.append(f"{axis} {a:.3f}->{b:.3f}")
    return out


def _axis_names(deltas: Sequence[str]) -> str:
    return ", ".join(d.split(" ")[0] for d in deltas)


def _with_feedback(prompt: str, feedback: Sequence[str]) -> str:
    """Anexa os motivos de descarte anteriores — correção dirigida, não repetição."""
    if not feedback:
        return prompt
    lines = "\n".join(f"- {f}" for f in feedback)
    return f"{prompt}\n\nTentativas anteriores REJEITADAS (não repita o mesmo erro):\n{lines}"


def _reason(n: int, agg: Aggregate, prev: Aggregate) -> str:
    ganhos = sorted(a for a, lo in agg.lower.items() if lo > prev.lower.get(a, 0.0))
    cmp = ">" if agg.overall > prev.overall else "<="
    return f"v{n}: overall {agg.overall:.3f} {cmp} {prev.overall:.3f}; ganhos em {ganhos or ['—']}"


def _inner_proposal(artifact: str, win: TuneVersion, outcome: TuneOutcome):
    """A proposta no formato da ação que já existe para aquela zona.

    Workflow passa pelo `propose_workflow` (que recompila a spec); skill vira
    `ResearchProposal`, que é o que a zona `skills/**` já sabe descrever.
    """
    key = Path(artifact).as_posix()
    if key.startswith(workflow_action.WORKFLOWS_SUBDIR):
        return workflow_action.propose_workflow(PurePosixPath(key).stem, toml_text=win.text)
    slug = PurePosixPath(key).stem
    return research.ResearchProposal(
        topic=f"tuning de {slug}",
        kind="code",
        slug=slug,
        target_file=key,
        reasons=(f"winner:v{outcome.winner}", win.reason),
    )


def _persist(artifact: str, chain: Sequence[TuneVersion], md: str) -> Path:
    """Cadeia + relatório em `data/`. FORA do bundle, sempre: um arquivo novo lá
    dentro é `eval:unlisted-file` e derrubaria o próprio exame."""
    d = chain_dir(artifact)
    d.mkdir(parents=True, exist_ok=True)
    for v in chain:
        (d / f"v{v.version}.txt").write_text(v.text, encoding="utf-8")
        _persist_trials(d, v)
    rows = [
        {
            "version": v.version,
            "overall": v.agg.overall,
            "per_axis": {a: list(t) for a, t in v.agg.per_axis.items()},
            "reason": v.reason,
            "valid": v.valid,
            "retained": v.retained,
            "holdout_overall": v.holdout.overall if v.holdout.n else None,
            "train_overall": v.train.overall if v.train.n else None,
            # Por linha e não no topo: o arquivo é uma LISTA (o replay lê assim),
            # e cada versão foi medida sob a régua que valia quando ela rodou.
            "ruler_version": RULER_VERSION,
        }
        for v in chain
    ]
    payload = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    (d / CHAIN_FILE).write_text(payload, encoding="utf-8")
    (d / EVALUATION_FILE).write_text(_stamp_ruler(md), encoding="utf-8")
    return d


def _persist_trials(d: Path, v: TuneVersion) -> None:
    """Um jsonl por versão com o trial CRU: eixos, diagnóstico e a saída julgada.

    É o que transforma "v3 descartada" em uma pergunta respondível — sem a
    saída ao lado do veredito, descobrir por que o eixo caiu exige re-rodar o
    exame, e re-rodar não reproduz o que aquela versão respondeu. A saída é
    truncada porque o arquivo é para leitura humana, não é backup do artefato.
    """
    if not v.trials or not v.outputs:
        return
    lines = [
        json.dumps(
            {
                "case_id": r.case_id,
                "trial": r.trial,
                "axes": r.axes,
                "notes": r.notes,
                "output": out[:TRIAL_OUTPUT_MAX],
            },
            ensure_ascii=False,
        )
        # `strict`: veredito emparelhado com a saída errada seria pior que
        # arquivo nenhum — evidência que mente não é evidência.
        for r, out in zip(v.trials, v.outputs, strict=True)
    ]
    (d / f"v{v.version}{TRIALS_SUFFIX}").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stamp_ruler(md: str) -> str:
    """Carimba a versão da régua no cabeçalho do relatório.

    No relatório e não só no chain.json porque a manchete ("nota 0.301") é o que
    alguém cola em outro lugar, e nota sem a régua que a produziu não é
    comparável com a de amanhã.
    """
    lines = md.splitlines()
    if not lines:
        return md
    head = [lines[0], "", f"Régua determinística: `ruler_version={RULER_VERSION}`."]
    return "\n".join([*head, *lines[1:]]) + "\n"


# --------------------------------------------------------------------------- replay

REPLAY_FILE = "REPLAY.md"
# Acima disto, a nota gravada e a re-medida divergem de verdade — a régua mudou
# desde a gravação (diagnóstico esperado logo após uma mudança de scorer).
DRIFT_EPS = 1e-6


@dataclass(frozen=True)
class ReplayRow:
    """Uma versão da cadeia re-medida sob a régua ATUAL."""

    version: int
    stored_overall: float | None
    rescored_overall: float
    retained: bool
    flags: tuple[str, ...]


@dataclass(frozen=True)
class ReplayOutcome:
    """O veredito do replay: a cadeia gravada sobrevive à régua de hoje?"""

    artifact: str
    rows: tuple[ReplayRow, ...]
    ok: bool
    path: str


def replay_chain(artifact: str, *, trials: int | None = None) -> ReplayOutcome:
    """Re-pontua a cadeia gravada sob a régua atual e escreve `REPLAY.md`.

    Flags por versão: `invalid-now` (não passa mais no validate), `score-drift`
    (nota gravada != re-medida — a régua mudou desde a gravação) e
    `non-monotonic` (a sequência de retidas COLAPSA sob a régua atual). Bundle
    adulterado aborta antes de medir: re-pontuar contra exame violado não prova
    nada.
    """
    violations = verify_frozen(artifact)
    if violations:
        raise TuneAborted(violations)

    d = chain_dir(artifact)
    chain_file = d / CHAIN_FILE
    if not chain_file.is_file():
        raise TuneError(f"sem cadeia gravada em {d}")
    stored_rows = sorted(
        json.loads(chain_file.read_text(encoding="utf-8")), key=lambda r: r["version"]
    )

    adapter = tunable_for(artifact)
    cases = load_cases(artifact)
    weights = {c.id: c.weight for c in cases}

    rows: list[ReplayRow] = []
    prev_rescored: float | None = None  # só sobre as RETIDAS: é a cadeia que importa
    for row in stored_rows:
        ver = int(row["version"])
        vfile = d / f"v{ver}.txt"
        if not vfile.is_file():
            raise TuneError(f"cadeia sem texto gravado: {vfile}")
        text = vfile.read_text(encoding="utf-8")
        # Legado: antes de `retained` existir, `valid` fazia o papel dele.
        retained = bool(row.get("retained", row.get("valid", False)))
        flags: list[str] = []
        if adapter.validate(text):
            rescored = 0.0
            flags.append("invalid-now")
        else:
            rescored = _score(adapter, cases, text, trials, weights)[0].overall
        stored = row.get("overall")
        if stored is not None and abs(stored - rescored) > DRIFT_EPS:
            flags.append("score-drift")
        if retained:
            if prev_rescored is not None and prev_rescored >= rescored:
                flags.append("non-monotonic")
            prev_rescored = rescored
        rows.append(ReplayRow(ver, stored, rescored, retained, tuple(flags)))

    ok = all(not r.flags for r in rows)
    path = d / REPLAY_FILE
    path.write_text(_replay_md(artifact, rows, ok), encoding="utf-8")
    return ReplayOutcome(artifact=artifact, rows=tuple(rows), ok=ok, path=str(path))


def _replay_md(artifact: str, rows: Sequence[ReplayRow], ok: bool) -> str:
    flagged = sum(1 for r in rows if r.flags)
    lines = [
        f"# replay — {artifact}",
        "",
        "| v | retained | stored | rescored | flags |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        stored = f"{r.stored_overall:.3f}" if r.stored_overall is not None else "—"
        lines.append(
            f"| {r.version} | {r.retained} | {stored} | {r.rescored_overall:.3f} "
            f"| {', '.join(r.flags) or '—'} |"
        )
    lines += [
        "",
        "OK: cadeia monotônica sob a régua atual"
        if ok
        else f"COLAPSO: {flagged} versão(ões) sinalizada(s)",
        "",
    ]
    return "\n".join(lines)


def action():
    """A ação registrável — consultada por `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_tune, apply=apply_tune)
