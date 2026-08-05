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

import pytest

from harness import paths
from harness.evals import freeze
from harness.evals.bundle import EvalCase, bundle_dir
from harness.evals.report import NOT_SCORED, render_evaluation_md
from harness.evals.score import aggregate, score_trial
from harness.genome.genome import load as load_genome
from harness.improve import ROOT_ENV, tune
from harness.ledger import store

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


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Árvore isolada + os dois seams de LLM trocados por stub.

    O runner devolve o PRÓPRIO prompt: o prompt de caso carrega o texto do
    artefato dentro dele, então "a saída reflete o artefato" sai de graça e sem
    inventar um modelo de mentira que pontuaria diferente do real.
    """
    monkeypatch.setenv(paths.EVALS_DIR_ENV, str(tmp_path / "evals"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))

    def runner(prompt, *, model=None, max_usd=0.0):
        runner.prompts.append(prompt)
        return prompt

    def rewriter(prompt, *, model=None, max_usd=0.0):
        rewriter.prompts.append(prompt)
        return rewriter.payload.pop(0)

    runner.prompts, rewriter.prompts, rewriter.payload = [], [], []
    monkeypatch.setattr(tune, "_call_runner", runner)
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
    # Abortou ANTES de medir: bundle adulterado não gasta backend.
    assert env.runner.prompts == []


def test_tune_cadeia_monotonica(env):
    _skill_tree(env)
    env.rewriter.payload = [V2, V3]

    out = tune.run_tune(SKILL, trials=1)

    assert [v.version for v in out.chain] == [1, 2, 3]
    assert out.winner == 2
    assert out.winning.text == V2
    # A v3 foi MEDIDA e perdeu no gate (não foi barrada por validade): o
    # motivo cita o placar dos dois lados.
    assert out.chain[2].valid is False
    assert out.chain[2].agg.n == 1
    assert out.chain[2].reason == (
        f"v3 descartada: overall {out.chain[2].agg.overall:.3f} <= "
        f"{out.chain[1].agg.overall:.3f}"
    )
    assert out.baseline["tuned"].overall > out.baseline["none"].overall
    # A cadeia inteira em data/, com o descartado junto: o motivo de parar é
    # parte da evidência.
    d = tune.chain_dir(SKILL)
    assert (d / "v2.txt").read_text(encoding="utf-8") == V2
    rows = json.loads((d / "chain.json").read_text(encoding="utf-8"))
    assert [r["valid"] for r in rows] == [True, True, False]
    assert (d / "EVALUATION.md").is_file()


def test_tune_versao_invalida_nao_e_scorada(env):
    _skill_tree(env)
    env.rewriter.payload = ["isto não tem frontmatter nenhum\n"]

    out = tune.run_tune(SKILL, trials=1)

    assert out.winner == 1
    assert out.chain[1].valid is False
    assert out.chain[1].agg.n == 0
    assert "skill ilegível" in out.chain[1].reason
    # 1 caso x 1 trial para o baseline `none` e 1 para a v1 — a v2 inválida não
    # chegou a custar chamada nenhuma.
    assert len(env.runner.prompts) == 2


def test_tune_workflow_usa_mesmo_caminho(env):
    spec = (REPO / WORKFLOW).read_text(encoding="utf-8")
    _tree(env.root, WORKFLOW, spec, WORKFLOW_CASES)

    out = tune.run_tune(WORKFLOW, rounds=0, trials=1)

    assert out.winner == 1
    assert out.winning.agg.overall > 0
    assert set(out.winning.agg.per_axis) == {"structure", "coverage"}
    # Medir workflow é render puro: zero LLM, nem para produzir nem para
    # reescrever.
    assert (env.runner.prompts, env.rewriter.prompts) == ([], [])
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
