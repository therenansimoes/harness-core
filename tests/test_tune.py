"""O loop de tuning: exame congelado, cadeia monotônica, veredito registrado.

Toda a árvore vive num `tmp_path` apontado por env (`HARNESS_EVALS_DIR`,
`HARNESS_ROOT`, `HARNESS_DATA_DIR`) pelo motivo do `test_evals_freeze`: sem
isso, `bundle_dir` resolveria o `evals/` REAL do checkout e um teste reescreveria
o manifest versionado — só que aqui seria pior, porque o tuning também GRAVA o
artefato.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import paths
from harness.backends import deepagents_backend
from harness.backends.registry import get_backend
from harness.evals import freeze
from harness.evals.bundle import EvalCase, bundle_dir, load_cases
from harness.evals.report import NOT_SCORED, render_evaluation_md
from harness.evals.score import aggregate, score_trial
from harness.genome.genome import load as load_genome
from harness.improve import ROOT_ENV, tune
from harness.improve.tunable import tunable_for
from harness.ledger import store
from harness.types import ExecResult

REPO = Path(__file__).resolve().parents[1]
REAL_GENOME = load_genome(REPO / "config" / "genome.toml")

SKILL = "skills/x.md"
WORKFLOW = "config/workflows/hotfix.toml"

SKILL_CASES = (
    '{"id":"s-1","kind":"code_fix","prompt":"como corrigir o import quebrado",'
    '"expect":{"must_mention":["alfa","beta"]},"axes":["grounding","coverage"],'
    '"weight":1.0,"trials":1}\n'
)
WORKFLOW_CASES = (
    '{"id":"w-1","kind":"workflow","prompt":"a espinha do hotfix",'
    '"expect":{"must_mention":["plan","execute","verify"]},'
    '"axes":["structure","coverage"],"weight":1.0,"trials":1}\n'
)


def _skill(*termos: str) -> str:
    corpo = "\n".join(f"- passo {t}" for t in termos) or "- passo nenhum"
    return (
        '---\nname = "x"\nkinds = ["code"]\n'
        'description = "orientação destilada: x"\n---\n\n'
        f"# guia\n{corpo}\n"
    )


V1, V2, V3 = _skill("alfa"), _skill("alfa", "beta"), _skill("gama")

# Guardado ANTES do fixture `env` trocar o seam: é o rewriter de verdade, o que
# decide entre modelo local e mock.
REAL_REWRITER = tune._call_rewriter


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Árvore isolada + os dois seams de LLM trocados por stub.

    O runner é um GRAVADOR sobre a medição extrativa real: devolve o próprio
    texto (o mesmo que `tune._run_case` faz) e anota o caso — assim os testes
    contam medições sem inventar um juiz diferente do de produção.
    """
    monkeypatch.setenv(paths.EVALS_DIR_ENV, str(tmp_path / "evals"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))

    def runner(text, case, *, model=None, max_usd=0.0):
        runner.calls.append(case.id)
        return text

    def rewriter(prompt, *, model=None, max_usd=0.0):
        rewriter.prompts.append(prompt)
        return rewriter.payload.pop(0)

    runner.calls, rewriter.prompts, rewriter.payload = [], [], []
    monkeypatch.setattr(tune, "_run_case", runner)
    monkeypatch.setattr(tune, "_call_rewriter", rewriter)

    class Env:
        pass

    e = Env()
    e.root, e.runner, e.rewriter = tmp_path, runner, rewriter
    return e


def _tree(root: Path, artifact: str, texto: str, cases: str) -> Path:
    art = root / artifact
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(texto, encoding="utf-8")
    d = bundle_dir(artifact)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.jsonl").write_text(cases, encoding="utf-8")
    freeze(artifact)
    return d


def _skill_tree(env, texto: str = V1) -> Path:
    return _tree(env.root, SKILL, texto, SKILL_CASES)


def test_tune_aborta_se_bundle_adulterado(env):
    d = _skill_tree(env)
    # Um caso a mais DEPOIS do freeze: o clássico "afrouxa a prova".
    (d / "cases.jsonl").write_text(SKILL_CASES + SKILL_CASES.replace("s-1", "s-2"), "utf-8")

    with pytest.raises(tune.TuneAborted) as exc:
        tune.run_tune(SKILL, trials=1)

    assert any("eval:modified" in v for v in exc.value.args[0])
    # Abortou ANTES de medir: bundle adulterado não pontua nada.
    assert env.runner.calls == []


def test_tune_cadeia_monotonica(env):
    _skill_tree(env)
    env.rewriter.payload = [V2, V3]

    out = tune.run_tune(SKILL, trials=1)

    assert [v.version for v in out.chain] == [1, 2, 3]
    assert out.winner == 2
    assert out.winning.text == V2
    # A v3 foi MEDIDA e perdeu no gate: continua VÁLIDA (passou no validate),
    # só não foi retida — o motivo cita o placar dos dois lados.
    assert out.chain[2].valid is True
    assert out.chain[2].retained is False
    assert out.chain[2].agg.n == 1
    assert out.chain[2].reason == (
        f"v3 descartada: overall {out.chain[2].agg.overall:.3f} <= {out.chain[1].agg.overall:.3f}"
    )
    assert out.baseline["tuned"].overall > out.baseline["none"].overall
    # A cadeia inteira em data/, com o descartado junto: o motivo de parar é
    # parte da evidência.
    d = tune.chain_dir(SKILL)
    assert (d / "v2.txt").read_text(encoding="utf-8") == V2
    rows = json.loads((d / "chain.json").read_text(encoding="utf-8"))
    assert [r["valid"] for r in rows] == [True, True, True]
    assert [r["retained"] for r in rows] == [True, True, False]
    # Bundle de caso único não tem holdout: a coluna existe, mas vazia.
    assert rows[2]["holdout_overall"] is None
    assert (d / "EVALUATION.md").is_file()


def test_tune_versao_invalida_nao_e_scorada(env):
    _skill_tree(env)
    env.rewriter.payload = ["isto não tem frontmatter nenhum\n"]

    # rounds=1 para o mínimo: com o default (2) o descarte NÃO encerra o loop e
    # haveria uma segunda tentativa.
    out = tune.run_tune(SKILL, rounds=1, trials=1)

    assert out.winner == 1
    assert len(out.chain) == 2
    assert out.chain[1].valid is False
    assert out.chain[1].retained is False
    assert out.chain[1].agg.n == 0
    assert "skill ilegível" in out.chain[1].reason
    # 1 caso x 1 trial para o baseline `none` e 1 para a v1 — a v2 inválida não
    # chegou a ser medida.
    assert len(env.runner.calls) == 2


def test_tune_workflow_usa_mesmo_caminho(env):
    spec = (REPO / WORKFLOW).read_text(encoding="utf-8")
    _tree(env.root, WORKFLOW, spec, WORKFLOW_CASES)

    out = tune.run_tune(WORKFLOW, rounds=0, trials=1)

    assert out.winner == 1
    assert out.winning.agg.overall > 0
    assert set(out.winning.agg.per_axis) == {"structure", "coverage"}
    # Medir workflow é render puro: zero LLM, nem para produzir nem para
    # reescrever.
    assert (env.runner.calls, env.rewriter.prompts) == ([], [])
    assert (tune.chain_dir(WORKFLOW) / "chain.json").is_file()


def test_tune_grava_mutation_com_reason(env):
    _skill_tree(env)
    env.rewriter.payload = [V2, V3]

    proposal = tune.propose_tune(SKILL, trials=1)
    record = tune.apply_tune(proposal, root=env.root, genome=REAL_GENOME)

    assert record.verdict == tune.PROMOTED
    assert "passo beta" in (env.root / SKILL).read_text(encoding="utf-8")

    rows = store.mutations(rule_id=f"tune:{SKILL}")
    assert len(rows) == 1
    assert (rows[0].arm_a, rows[0].arm_b) == ("v1", "v2")
    assert rows[0].action == "tune"
    assert rows[0].note == proposal.outcome.winning.reason
    assert "overall" in rows[0].note


def test_evaluation_md_substitui_not_scored(env):
    _skill_tree(env)
    case = EvalCase(id="s-1", kind="code_fix", prompt="p", axes=("structure",))
    agg = aggregate([score_trial(case, "# ok\n- feito\n", trial=i) for i in range(4)])

    cru = render_evaluation_md(SKILL)
    medido = render_evaluation_md(SKILL, scores=agg, baseline={"none": agg, "tuned": agg})

    assert NOT_SCORED in cru
    assert "## scores" not in cru
    assert NOT_SCORED not in medido
    assert f"| structure | 4 | 4 | {agg.lower['structure']:.3f} |" in medido
    assert f"baseline: none {agg.overall:.3f} · tuned {agg.overall:.3f}" in medido


# --------------------------------------------------------------------------- rewriter real


def _no_lmstudio(monkeypatch):
    """Servidor local morto, do jeito que o preflight vê: a sonda levanta."""

    def boom(url):
        raise OSError("connection refused")

    monkeypatch.setattr(deepagents_backend, "_lmstudio_models", boom)


def _exec_result(req, *, ok: bool, exit_reason: str) -> ExecResult:
    return ExecResult(
        ok=ok,
        exit_reason=exit_reason,
        turns=1,
        cost_usd=0.0,
        tokens_in=0,
        tokens_out=0,
        files_changed=(),
        session_id=None,
        trace_path=req.trace_path,
    )


class _FakeReal:
    """O backend real, de mentira: guarda o pedido e escreve (ou falha)."""

    def __init__(self, fail: bool = False) -> None:
        self.fail, self.reqs = fail, []

    def preflight(self):
        return SimpleNamespace(ok=True, reason="fake")

    def execute(self, req):
        self.reqs.append(req)
        if self.fail:
            return _exec_result(req, ok=False, exit_reason="error")
        # Deixado FORA de `files_changed` de propósito: quem lê o artefato é o
        # `out_file`, não o que o backend declarou ter mexido.
        (req.workspace / tune.REWRITE_FILE).write_text(V2, encoding="utf-8")
        return _exec_result(req, ok=True, exit_reason="done")


def _route(monkeypatch, fake: _FakeReal) -> None:
    """`deepagents` vira o fake; `mock` continua sendo o mock de verdade — é ele
    o fallback que o teste quer exercitar."""
    monkeypatch.setattr(tune, "_rewrite_target", lambda model: (tune.REWRITE_BACKEND, "openai:m"))
    monkeypatch.setattr(
        tune,
        "get_backend",
        lambda name: fake if name == tune.REWRITE_BACKEND else get_backend(name),
    )


def test_rewrite_target_sem_lm_studio_cai_no_mock(monkeypatch):
    _no_lmstudio(monkeypatch)

    assert tune._rewrite_target(tune.DEFAULT_MODEL) == (tune.TUNE_BACKEND, tune.DEFAULT_MODEL)
    assert tune._rewrite_target(None) == (tune.TUNE_BACKEND, None)


def test_rewrite_target_troca_modelo_grande_antes_de_sondar(monkeypatch):
    sondados = []

    class Probe:
        def __init__(self, model=None):
            sondados.append(model)

        def preflight(self):
            return SimpleNamespace(ok=True, reason="fake")

    monkeypatch.setattr(deepagents_backend, "DeepagentsBackend", Probe)

    alvo = tune._rewrite_target("openai:qwen/qwen3-coder-30b")

    # Modelo que trava a máquina nem chega a ser sondado: vira o default.
    assert alvo == (tune.REWRITE_BACKEND, tune.REWRITE_MODEL)
    assert sondados == [tune.REWRITE_MODEL]


def test_rewriter_real_le_o_artefato_do_arquivo(monkeypatch):
    fake = _FakeReal()
    _route(monkeypatch, fake)

    texto = tune._call_rewriter("reescreva", model=None, max_usd=1.0)

    assert texto == V2.strip()
    (req,) = fake.reqs
    assert req.tools == ("read_file", "write_file")
    assert req.timeout_s == tune.REWRITE_TIMEOUT_S
    assert req.max_turns > 1  # escrever arquivo custa mais de um turno
    assert tune.REWRITE_FILE in req.prompt


def test_rewriter_real_falhando_cai_no_mock(monkeypatch):
    fake = _FakeReal(fail=True)
    _route(monkeypatch, fake)

    texto = tune._call_rewriter("reescreva", model=None, max_usd=1.0)

    # O mock devolve o prompt de hoje — sem a ordem de gravar `rewrite.out`,
    # que só faz sentido para quem tem tool de escrita.
    assert texto == "reescreva"
    assert tune.REWRITE_FILE not in texto
    assert len(fake.reqs) == 1


def test_tune_sem_lm_studio_roda_a_cadeia_de_hoje(env, monkeypatch):
    """Aceite da frente C: LM Studio fora do ar não muda nada do que já rodava."""
    _no_lmstudio(monkeypatch)
    monkeypatch.setattr(tune, "_call_rewriter", REAL_REWRITER)
    _skill_tree(env)

    out = tune.run_tune(SKILL, trials=1)

    # Mock devolvendo o próprio prompt: nenhuma candidata é skill válida, mas o
    # descarte NÃO encerra o loop — rounds=2 => duas tentativas registradas e o
    # vencedor continua sendo a v1.
    assert out.winner == 1
    assert len(out.chain) == 3
    assert [v.valid for v in out.chain] == [True, False, False]
    assert out.chain[1].text.startswith("Reescreva o artefato")


# --------------------------------------------------------------------------- rounds/feedback/holdout


def test_rounds_sao_tentativas_e_feedback_acumula(env):
    """Descartada não encerra o loop; o motivo dela entra no prompt seguinte."""
    _skill_tree(env)
    env.rewriter.payload = ["sem frontmatter\n", V3, V2]

    out = tune.run_tune(SKILL, rounds=3, trials=1)

    assert [v.version for v in out.chain] == [1, 2, 3, 4]
    assert out.winner == 4
    assert len(env.rewriter.prompts) == 3
    assert "Tentativas anteriores REJEITADAS" not in env.rewriter.prompts[0]
    assert "Tentativas anteriores REJEITADAS" in env.rewriter.prompts[1]
    assert "v2 descartada" in env.rewriter.prompts[1]
    assert "v2 descartada" in env.rewriter.prompts[2]
    assert "v3 descartada" in env.rewriter.prompts[2]


def test_feedback_zera_apos_retencao(env):
    """Retenção troca a base — feedback das tentativas antigas expira junto."""
    _skill_tree(env)
    env.rewriter.payload = [V3, V2, V3]

    out = tune.run_tune(SKILL, rounds=3, trials=1)

    assert out.winner == 3
    assert "Tentativas anteriores REJEITADAS" in env.rewriter.prompts[1]
    assert "Tentativas anteriores REJEITADAS" not in env.rewriter.prompts[2]


# Dois casos de trial único com baldes conhecidos: s-1 (balde 0) cai no holdout,
# s-2 (balde 3) no treino. O peso maior do treino faz a candidata "só treino"
# ganhar no overall cheio — é o holdout que tem que barrá-la.
HOLDOUT_CASES = (
    '{"id":"s-1","kind":"code_fix","prompt":"caso holdout",'
    '"expect":{"must_mention":["alfa"]},"axes":["coverage"],"weight":1.0,"trials":1}\n'
    '{"id":"s-2","kind":"code_fix","prompt":"caso treino",'
    '"expect":{"must_mention":["beta"]},"axes":["coverage"],"weight":2.0,"trials":1}\n'
)


def test_holdout_gate_barra_regressao(env):
    _tree(env.root, SKILL, _skill("alfa"), HOLDOUT_CASES)
    env.rewriter.payload = [_skill("beta"), _skill("alfa", "beta")]

    out = tune.run_tune(SKILL, rounds=2, trials=1)

    # v2 melhora o treino mas colapsa o holdout: overall cheio sobe e mesmo
    # assim ela é barrada — com o motivo certo.
    assert out.chain[1].valid is True
    assert out.chain[1].retained is False
    assert "holdout" in out.chain[1].reason
    # v3 melhora sem regredir o holdout: retida.
    assert out.winner == 3
    assert out.chain[2].retained is True


def test_scorer_discrimina_no_bundle_real(env):
    """Aceite (b): artefato visivelmente mais rico pontua mais no bundle REAL."""
    artifact = "skills/python-fixes.md"
    original = (REPO / artifact).read_text(encoding="utf-8")
    _tree(
        env.root,
        artifact,
        original,
        (REPO / "evals/skills/python-fixes/cases.jsonl").read_text(encoding="utf-8"),
    )
    enriched = original + (
        "\n- ModuleNotFoundError em tests/: ajuste sys.path via conftest.py na raiz do repo.\n"
        "- dataclass frozen=True: use dataclasses.replace, nunca setattr.\n"
        "- Pedido para desabilitar teste que falha: recuso — o teste é o contrato.\n"
        "- Verificação executável: rode pytest -x e só declare pronto com saída verde.\n"
    )
    adapter = tunable_for(artifact)
    cases = load_cases(artifact)
    weights = {c.id: c.weight for c in cases}

    a0 = tune._score(adapter, cases, original, 1, weights)[0]
    a1 = tune._score(adapter, cases, enriched, 1, weights)[0]

    assert a1.overall > a0.overall
    # Estritamente melhor em pelo menos um eixo, não só na média.
    assert any(a1.lower[ax] > a0.lower.get(ax, 0.0) for ax in a1.lower)


# --------------------------------------------------------------------------- replay


def test_replay_chain(env):
    _skill_tree(env)
    env.rewriter.payload = [V2, V3]
    tune.run_tune(SKILL, trials=1)

    rep = tune.replay_chain(SKILL, trials=1)

    assert rep.ok is True
    assert (tune.chain_dir(SKILL) / "REPLAY.md").is_file()

    # Nota gravada adulterada => drift: a régua atual não reproduz o placar.
    cj = tune.chain_dir(SKILL) / "chain.json"
    rows = json.loads(cj.read_text(encoding="utf-8"))
    rows[1]["overall"] = 0.999
    cj.write_text(json.dumps(rows), encoding="utf-8")
    rep2 = tune.replay_chain(SKILL, trials=1)
    assert rep2.ok is False
    assert "score-drift" in rep2.rows[1].flags

    # Cadeia gravada "de trás para frente": as retidas colapsam sob a régua.
    d = tune.chain_dir(SKILL)
    (d / "v1.txt").write_text(V2, encoding="utf-8")
    (d / "v2.txt").write_text(V1, encoding="utf-8")
    (d / "chain.json").write_text(
        json.dumps([{"version": 1, "retained": True}, {"version": 2, "retained": True}]),
        encoding="utf-8",
    )
    rep3 = tune.replay_chain(SKILL, trials=1)
    assert rep3.ok is False
    assert "non-monotonic" in rep3.rows[1].flags


def test_replay_sem_cadeia(env):
    _skill_tree(env)

    with pytest.raises(tune.TuneError):
        tune.replay_chain(SKILL)
