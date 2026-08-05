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
2. GATE MONOTÔNICO. Uma versão só entra na cadeia se BATER a anterior; a
   primeira que não bater encerra a cadeia e o vencedor é a última que bateu.
   Sem isso, "3 rodadas" vira "a última rodada", que é aleatória.
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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from harness import paths
from harness.backends.registry import get_backend
from harness.evals.bundle import EvalCase, load_cases
from harness.evals.freeze import verify_frozen
from harness.evals.report import render_evaluation_md
from harness.evals.score import Aggregate, TrialResult, aggregate, beats, score_trial
from harness.improve import mutate, research, workflow_action
from harness.improve.tunable import Tunable, tunable_for
from harness.ledger import store
from harness.types import ExecRequest, MutationRow

ACTION = "tune"
TUNE_SUBDIR = "tune"
CHAIN_FILE = "chain.json"
EVALUATION_FILE = "EVALUATION.md"

# Mock por default pelo mesmo motivo do resto do pacote: o caminho feliz de
# quem só quer ver o loop rodar não pode exigir chave de API.
TUNE_BACKEND = "mock"
TUNE_TIMEOUT_S = 600.0
DEFAULT_MODEL = "openai:qwen/qwen3.5-9b"
DEFAULT_MAX_USD = 0.25
# 2 reescritas = até v3. Mais que isso raramente muda o vencedor e sempre muda
# a fatura; quem quiser cadeia longa passa `rounds`.
DEFAULT_ROUNDS = 2

PROMOTED = "promoted"
HELD = "held"

_EMPTY = Aggregate(per_axis={}, lower={}, overall=0.0, n=0)


class TuneAborted(Exception):
    """Exame adulterado: nada foi medido e nada será gravado."""


class TuneError(Exception):
    """Backend falhou, estourou o teto ou devolveu texto vazio."""


@dataclass(frozen=True)
class TuneVersion:
    """Um elo da cadeia. `valid=False` => `agg` é o placar vazio, não um zero
    medido: a versão nem chegou a ser pontuada."""

    version: int
    text: str
    agg: Aggregate
    reason: str
    valid: bool = True
    trials: tuple[TrialResult, ...] = field(default=(), repr=False)


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
    weights = {c.id: c.weight for c in cases}

    # Controle: o que sai SEM artefato nenhum. Passa por fora do `validate` de
    # propósito — texto vazio nunca é uma skill válida, e barrá-lo aqui mataria
    # justamente a medição que diz se o artefato paga o próprio custo.
    none_agg, _ = _score(adapter, cases, "", trials, weights)

    chain: list[TuneVersion] = []
    text = candidate if candidate is not None else adapter.read()
    prev = none_agg
    winner = 1

    for n in range(1, rounds + 2):
        errs = adapter.validate(text)
        if errs:
            chain.append(TuneVersion(n, text, _EMPTY, f"v{n} descartada: {errs[0]}", valid=False))
            break
        agg, results = _score(adapter, cases, text, trials, weights)
        # v1 é o incumbente (é o que já está no disco), não uma candidata: o
        # gate julga de v2 em diante. Recusar v1 por não bater o `none` deixaria
        # o loop sem ponto de partida e sem nada a reescrever.
        if n > 1 and not beats(agg, prev):
            chain.append(
                TuneVersion(
                    n,
                    text,
                    agg,
                    f"v{n} descartada: overall {agg.overall:.3f} <= {prev.overall:.3f}",
                    valid=False,
                    trials=tuple(results),
                )
            )
            break
        chain.append(
            TuneVersion(n, text, agg, _reason(n, agg, prev), valid=True, trials=tuple(results))
        )
        winner, prev = n, agg
        if n == rounds + 1:
            break
        text = _call_rewriter(
            adapter.rewrite_prompt(text, _weak(results)), model=model, max_usd=max_usd
        )

    win = chain[winner - 1]
    baseline = {"none": none_agg, "draft": chain[0].agg, "tuned": win.agg}
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


def _call_runner(prompt: str, *, model: str | None, max_usd: float) -> str:
    """UMA chamada ao backend para produzir a saída de um caso. Seam de teste."""
    return _call(prompt, model=model, max_usd=max_usd, purpose="run")


def _call_rewriter(prompt: str, *, model: str | None, max_usd: float) -> str:
    """UMA chamada ao backend para reescrever o artefato. Seam de teste."""
    return _call(prompt, model=model, max_usd=max_usd, purpose="rewrite")


def _call(
    prompt: str, *, model: str | None, max_usd: float, purpose: str, backend: str = TUNE_BACKEND
) -> str:
    """O padrão do `add._call_author`: workspace efêmero, teto de custo, texto
    ou erro — nunca string vazia passando por resposta."""
    b = get_backend(backend)
    pre = b.preflight()
    if not pre.ok:
        raise TuneError(f"backend {backend} indisponível: {pre.reason}")
    ws = Path(tempfile.mkdtemp(prefix=f"harness-tune-{purpose}-"))
    trace = ws / "trace.jsonl"
    try:
        result = b.execute(
            ExecRequest(
                prompt=prompt,
                workspace=ws / "out",
                tools=("Read",),
                model=model,
                max_turns=1,
                timeout_s=TUNE_TIMEOUT_S,
                trace_path=trace,
            )
        )
        if not result.ok:
            raise TuneError(f"chamada de {purpose} falhou: exit_reason={result.exit_reason}")
        if result.cost_usd is not None and result.cost_usd > max_usd:
            raise TuneError(
                f"{purpose} custou ${result.cost_usd:.4f} > teto ${max_usd:.2f} — cadeia parada"
            )
        text = _result_text(trace) or _workspace_text(ws / "out", result.files_changed)
        if not text:
            raise TuneError(f"backend {backend!r} não devolveu texto de {purpose}")
        return text
    finally:
        shutil.rmtree(ws, ignore_errors=True)


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
) -> tuple[Aggregate, list[TrialResult]]:
    """Roda `trials` tentativas por caso e agrega. `trials=None` = o do caso."""
    results: list[TrialResult] = []
    for case in cases:
        n = trials if trials is not None else case.trials
        for i in range(max(1, n)):
            results.append(score_trial(case, adapter.produce(case, text), trial=i))
    return aggregate(results, weights), results


def _weak(results: Sequence[TrialResult]) -> list[TrialResult]:
    """Só os trials com pelo menos um eixo reprovado — é o que o reescritor lê."""
    return [r for r in results if not all(r.axes.values())]


def _reason(n: int, agg: Aggregate, prev: Aggregate) -> str:
    ganhos = sorted(a for a, lo in agg.lower.items() if lo > prev.lower.get(a, 0.0))
    cmp = ">" if agg.overall > prev.overall else "<="
    return (
        f"v{n}: overall {agg.overall:.3f} {cmp} {prev.overall:.3f}; "
        f"ganhos em {ganhos or ['—']}"
    )


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
    rows = [
        {
            "version": v.version,
            "overall": v.agg.overall,
            "per_axis": {a: list(t) for a, t in v.agg.per_axis.items()},
            "reason": v.reason,
            "valid": v.valid,
        }
        for v in chain
    ]
    payload = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    (d / CHAIN_FILE).write_text(payload, encoding="utf-8")
    (d / EVALUATION_FILE).write_text(md, encoding="utf-8")
    return d


def action():
    """A ação registrável — consultada por `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_tune, apply=apply_tune)
