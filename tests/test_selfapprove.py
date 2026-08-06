"""O gate de auto-aprovação: decisão pura (`ruler.selfapprove`) + fiação
(`improve.selfapprove`).

Mesma isolação de árvore do `test_tune.py`: `HARNESS_EVALS_DIR`,
`HARNESS_DATA_DIR`, `HARNESS_CONFIG_DIR` e `HARNESS_ROOT` apontam para
`tmp_path`, e os dois seams de LLM do `tune` são trocados por stub — aqui só
troca também `tune._probe_real`/`tune._run_case_real` para os testes de
runner=real, e cada árvore ganha o `config/genome.toml` REAL do repo (copiado,
não reinventado) e um `config/selfapprove.toml` próprio — a "árvore protegida"
é uma árvore de verdade, não um atalho de teste.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import cli, paths
from harness.evals.bundle import bundle_dir, load_cases
from harness.evals.freeze import freeze, verify_frozen
from harness.evals.score import Aggregate, TrialResult
from harness.genome.genome import check_patch
from harness.genome.genome import load as load_genome
from harness.improve import ROOT_ENV, rollback, tune
from harness.improve import selfapprove as sa
from harness.ledger import store
from harness.ruler import selfapprove as ruler_sa

REPO = Path(__file__).resolve().parents[1]
REAL_GENOME = load_genome(REPO / "config" / "genome.toml")
GENOME_TEXT = (REPO / "config" / "genome.toml").read_text(encoding="utf-8")

SKILL = "skills/x.md"

CASES = (
    '{"id":"c-1","kind":"code_fix","prompt":"p1","expect":{"must_mention":["alfa"]},'
    '"axes":["grounding","coverage"],"weight":1.0,"trials":1}\n'
    '{"id":"c-2","kind":"code_fix","prompt":"p2","expect":{"must_mention":["beta"]},'
    '"axes":["grounding","coverage"],"weight":1.0,"trials":1}\n'
    '{"id":"c-3","kind":"code_fix","prompt":"p3","expect":{"must_mention":["gama"]},'
    '"axes":["grounding","coverage"],"weight":1.0,"trials":1}\n'
    '{"id":"c-4","kind":"code_fix","prompt":"p4","expect":{"must_mention":["delta"]},'
    '"axes":["grounding","coverage"],"weight":1.0,"trials":1}\n'
)
# case_bucket("c-4") == 0 -> holdout; c-1/c-2/c-3 -> treino. Escolhido a dedo
# (mesma técnica do bundle real: 4 casos, split 3 treino / 1 holdout).


def _skill(*termos: str) -> str:
    corpo = "\n".join(f"- passo {t}" for t in termos) or "- passo nenhum"
    return (
        '---\nname = "x"\nkinds = ["code"]\n'
        'description = "orientação destilada: x"\n---\n\n'
        f"# guia\n{corpo}\n"
    )


V1 = _skill("nada")
V2 = _skill("alfa", "beta", "gama", "delta")


def _selfapprove_toml(
    *,
    enabled: bool = True,
    require_measured: bool = True,
    min_real_trial_frac: float = 1.0,
    min_delta: float = 0.05,
    min_delta_vs_none: float = 0.0,
    min_cases: int = 4,
    min_holdout_cases: int = 1,
    min_trials: int = 2,
    min_holdout_trials: int = 1,
    external_enabled: bool = False,
    pinned: dict[str, str] | None = None,
) -> str:
    lines = [
        "[selfapprove]",
        f"enabled = {str(enabled).lower()}",
        f"require_measured_behavior = {str(require_measured).lower()}",
        f"min_real_trial_frac = {min_real_trial_frac}",
        f"min_delta = {min_delta}",
        f"min_delta_vs_none = {min_delta_vs_none}",
        f"min_cases = {min_cases}",
        f"min_holdout_cases = {min_holdout_cases}",
        f"min_trials = {min_trials}",
        f"min_holdout_trials = {min_holdout_trials}",
        "holdout_tolerance = 0.0",
        "",
        "[selfapprove.external]",
        f"enabled = {str(external_enabled).lower()}",
        "",
        "[selfapprove.pinned]",
    ]
    for artifact, pin in (pinned or {}).items():
        lines.append(f'"{artifact}" = "{pin}"')
    return "\n".join(lines) + "\n"


def _write_selfapprove(root: Path, **kw) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "selfapprove.toml").write_text(_selfapprove_toml(**kw), encoding="utf-8")


def _write_genome(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "genome.toml").write_text(GENOME_TEXT, encoding="utf-8")


def _tree(root: Path, artifact: str, texto: str, cases: str):
    art = root / artifact
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(texto, encoding="utf-8")
    d = bundle_dir(artifact)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.jsonl").write_text(cases, encoding="utf-8")
    return freeze(artifact)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.EVALS_DIR_ENV, str(tmp_path / "evals"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))

    _write_genome(tmp_path)
    _write_selfapprove(tmp_path)  # default: ligado, nada pinado

    def runner(text, case, *, model=None, max_usd=0.0):
        runner.calls.append(case.id)
        return text

    def rewriter(prompt, *, model=None, max_usd=0.0):
        rewriter.prompts.append(prompt)
        return rewriter.payload.pop(0)

    runner.calls, rewriter.prompts, rewriter.payload = [], [], []
    monkeypatch.setattr(tune, "_run_case", runner)
    monkeypatch.setattr(tune, "_call_rewriter", rewriter)
    tune.reset_real_counters()
    tune.reset_spend()

    class Env:
        pass

    e = Env()
    e.root, e.runner, e.rewriter = tmp_path, runner, rewriter
    return e


def _stub_ok(text, case, *, model=None, max_usd=0.0):
    """Comportamento do modelo é função das âncoras que a orientação carrega —
    skill sem âncora produz resposta sem âncora, e vice-versa."""
    tune._REAL["ok"] += 1
    anchors = [a for a in ("alfa", "beta", "gama", "delta") if a in text]
    return "resposta com " + " ".join(anchors) if anchors else "sem orientação"


def _real_stub(*, fallback_case: str | None = None):
    def stub(text, case, *, model=None, max_usd=0.0):
        if fallback_case is not None and case.id == fallback_case:
            tune._REAL["fallback"] += 1
            return tune._run_case(text, case, model=model, max_usd=max_usd)
        return _stub_ok(text, case, model=model, max_usd=max_usd)

    return stub


# =========================================================================== decide/stamp (puro)


def _ev(**over) -> ruler_sa.Evidence:
    base = dict(
        artifact="skills/x.md",
        target_file="skills/x.md",
        origin=ruler_sa.INTERNAL,
        winner=2,
        winner_valid=True,
        runner=ruler_sa.RUNNER_REAL,
        probe=ruler_sa.PROBE_OK,
        real_ok=8,
        real_fallback=0,
        draft_is_incumbent=True,
        eval_violations=(),
        bundle_version=1,
        bundle_sha256="a" * 64,
        measure_error="",
        security_ran=True,
        security_findings=(),
        before_overall=0.2,
        after_overall=0.4,
        none_overall=0.1,
        before_holdout=0.2,
        after_holdout=0.3,
        n_total=8,
        n_holdout=2,
        n_cases=4,
        n_holdout_cases=1,
        ruler_version=2,
        cost_usd=0.0,
        backend_calls=8,
        cost_unknown=0,
    )
    base.update(over)
    return ruler_sa.Evidence(**base)


def _th(**over) -> ruler_sa.Thresholds:
    base = dict(enabled=True, pinned=(("skills/x.md", "v1:" + "a" * 64),))
    base.update(over)
    return ruler_sa.Thresholds(**base)


def test_decide_ativa_no_caminho_feliz():
    v = ruler_sa.decide(_ev(), _th())
    assert v.decision == ruler_sa.ACTIVATE
    assert v.reason.startswith("selfapprove:ok:")
    assert v.threshold == _th().min_delta


def test_decide_sem_candidata_e_no_change():
    v = ruler_sa.decide(_ev(winner=1), _th())
    assert (v.decision, v.reason) == (ruler_sa.NO_CHANGE, "selfapprove:no-candidate")


def test_decide_vencedor_invalido():
    v = ruler_sa.decide(_ev(winner_valid=False), _th())
    assert (v.decision, v.reason) == (ruler_sa.HUMAN_QUEUE, "selfapprove:invalid-winner")


def test_decide_desligado_por_config():
    v = ruler_sa.decide(_ev(), _th(enabled=False))
    assert (v.decision, v.reason) == (ruler_sa.HUMAN_QUEUE, "selfapprove:off")


def test_decide_externo_sem_optin():
    v = ruler_sa.decide(_ev(origin=ruler_sa.EXTERNAL), _th())
    assert v.reason == "selfapprove:external-optin-off"

    v2 = ruler_sa.decide(_ev(origin=ruler_sa.EXTERNAL), _th(external_enabled=True))
    assert v2.decision == ruler_sa.ACTIVATE


def test_decide_achado_de_seguranca_vence_delta_grande():
    v = ruler_sa.decide(
        _ev(security_findings=("secret",), after_overall=0.99, before_overall=0.0), _th()
    )
    assert v.reason == "selfapprove:security:secret"


def test_decide_seguranca_nao_rodou():
    v = ruler_sa.decide(_ev(security_ran=False), _th())
    assert v.reason == "selfapprove:security-not-run"


def test_decide_exame_adulterado():
    v = ruler_sa.decide(_ev(eval_violations=("eval:modified:cases.jsonl",)), _th())
    assert v.reason == "selfapprove:eval-tampered:eval:modified:cases.jsonl"


def test_decide_measure_error():
    v = ruler_sa.decide(_ev(measure_error="bundle-missing"), _th())
    assert v.reason == "selfapprove:measure-error:bundle-missing"


def test_decide_bundle_sem_pin():
    v = ruler_sa.decide(_ev(), _th(pinned=()))
    assert v.reason == "selfapprove:bundle-unpinned"


def test_decide_bundle_pin_divergente():
    v = ruler_sa.decide(_ev(bundle_sha256="b" * 64), _th())
    assert v.reason.startswith("selfapprove:bundle-changed:")


def test_decide_draft_nao_e_incumbente():
    v = ruler_sa.decide(_ev(draft_is_incumbent=False), _th())
    assert v.reason == "selfapprove:draft-not-incumbent"


def test_decide_comportamento_nao_medido():
    assert ruler_sa.decide(_ev(runner="extractive"), _th()).reason == "selfapprove:behavior-not-measured"
    assert (
        ruler_sa.decide(_ev(runner="extractive(fallback:probe)"), _th()).reason
        == "selfapprove:behavior-not-measured"
    )


def test_decide_comportamento_parcial():
    v = ruler_sa.decide(_ev(real_ok=12, real_fallback=4), _th())
    assert v.reason == "selfapprove:behavior-partial:0.75<1.00"


def test_decide_poucos_casos():
    v = ruler_sa.decide(_ev(n_cases=2), _th())
    assert v.reason == "selfapprove:cases-too-few:n=2<4"


def test_decide_poucos_casos_de_holdout():
    v = ruler_sa.decide(_ev(n_holdout_cases=0), _th())
    assert v.reason == "selfapprove:holdout-cases-too-few:n=0<1"


def test_decide_amostra_pequena():
    v = ruler_sa.decide(_ev(n_total=1), _th())
    assert v.reason == "selfapprove:sample-too-small:n=1<8"


def test_decide_holdout_pequeno():
    v = ruler_sa.decide(_ev(n_holdout=0), _th())
    assert v.reason == "selfapprove:holdout-too-small:n=0<2"


def test_decide_holdout_regrediu():
    v = ruler_sa.decide(_ev(before_holdout=0.5, after_holdout=0.4), _th())
    assert v.reason == "selfapprove:holdout-regressed:0.500->0.400"


def test_decide_sem_ganho_vs_none():
    v = ruler_sa.decide(_ev(after_overall=0.1, none_overall=0.1), _th())
    assert v.reason == "selfapprove:no-lift-vs-none:0.100<=0.100"


def test_decide_abaixo_do_limiar():
    v = ruler_sa.decide(_ev(after_overall=0.22, before_overall=0.2), _th())
    assert v.reason.startswith("selfapprove:below-threshold:")


def test_decide_nunca_rejeita_matriz():
    variacoes = [
        {},
        {"winner": 1},
        {"winner_valid": False},
        {"security_findings": ("secret",)},
        {"eval_violations": ("x",)},
        {"measure_error": "io"},
        {"draft_is_incumbent": False},
        {"runner": "extractive"},
        {"real_ok": 1, "real_fallback": 9},
        {"n_cases": 0},
        {"n_total": 0},
        {"before_holdout": 1.0, "after_holdout": 0.0},
        {"after_overall": 0.0, "none_overall": 0.0},
    ]
    for over in variacoes:
        v = ruler_sa.decide(_ev(**over), _th())
        assert v.decision in (ruler_sa.ACTIVATE, ruler_sa.HUMAN_QUEUE, ruler_sa.NO_CHANGE)
        assert v.reason.startswith("selfapprove:")


def test_stamp_string_exata():
    ev = ruler_sa.Evidence(
        artifact="skills/python-fixes.md",
        target_file="skills/python-fixes.md",
        origin="internal",
        winner=2,
        winner_valid=True,
        runner="real",
        probe="ok",
        real_ok=16,
        real_fallback=0,
        draft_is_incumbent=True,
        bundle_version=3,
        bundle_sha256="9f2a1c4b8e70" + "0" * 52,
        security_ran=True,
        before_overall=0.412,
        after_overall=0.588,
        none_overall=0.301,
        before_holdout=0.500,
        after_holdout=0.500,
        n_total=16,
        n_holdout=4,
        n_cases=4,
        n_holdout_cases=1,
        ruler_version=2,
        cost_usd=0.0,
        backend_calls=12,
    )
    th = ruler_sa.Thresholds(enabled=True, pinned=(("skills/python-fixes.md", ev.bundle_pin),))
    v = ruler_sa.decide(ev, th)
    assert v.stamp == (
        "before=0.412 after=0.588 delta=+0.176 min=0.050 none=0.301 hold=0.500->0.500 "
        "n=16/4 cases=4/1 real=16/0 runner=real probe=ok ruler=v2 bundle=v3:9f2a1c4b8e7000000 "
        "sec=ok cost=$0.0000/12 origin=internal -> activate"
    )


# =========================================================================== load_thresholds


def test_load_thresholds_env_flag_desliga(monkeypatch):
    monkeypatch.setenv(ruler_sa.ENV_FLAG, "0")
    assert ruler_sa.load_thresholds() == ruler_sa.Thresholds()


def test_load_thresholds_arquivo_ausente(tmp_path, monkeypatch):
    monkeypatch.setenv(ruler_sa.ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    assert ruler_sa.load_thresholds() == ruler_sa.Thresholds()


def test_load_thresholds_toml_invalido(tmp_path, monkeypatch):
    monkeypatch.setenv(ruler_sa.ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    _write_genome(tmp_path)
    (tmp_path / "config" / "selfapprove.toml").write_text("torto = [[[", encoding="utf-8")
    assert ruler_sa.load_thresholds() == ruler_sa.Thresholds()


def test_load_thresholds_genoma_nao_lista_arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv(ruler_sa.ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "selfapprove.toml").write_text("[selfapprove]\nenabled = true\n", "utf-8")
    (tmp_path / "config" / "genome.toml").write_text(
        'immutable = ["harness/ruler/**"]\nmutable = ["skills/**"]\n', encoding="utf-8"
    )
    assert ruler_sa.load_thresholds() == ruler_sa.Thresholds()


def test_load_thresholds_caminho_nao_confiavel(tmp_path, monkeypatch):
    """Cópia num `config/` de fora da árvore de `$HARNESS_ROOT` não concede nada."""
    monkeypatch.setenv(ruler_sa.ROOT_ENV, str(tmp_path / "root"))
    outro = tmp_path / "outro" / "config"
    outro.mkdir(parents=True)
    (outro / "selfapprove.toml").write_text("[selfapprove]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(outro))
    assert ruler_sa.load_thresholds() == ruler_sa.Thresholds()


def test_load_thresholds_coercao_de_campos(tmp_path, monkeypatch):
    monkeypatch.setenv(ruler_sa.ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    _write_genome(tmp_path)
    (tmp_path / "config" / "selfapprove.toml").write_text(
        "[selfapprove]\n"
        'enabled = "sim"\n'  # str -> default (False)
        "min_delta = -1.0\n"  # negativo -> default
        "min_real_trial_frac = 2.0\n"  # clampado, não default
        "\n"
        "[selfapprove.pinned]\n"
        '"skills/a.md" = "not-a-pin"\n'  # descartado
        '"skills/b.md" = "v1:' + "a" * 64 + '"\n',  # válido
        encoding="utf-8",
    )
    th = ruler_sa.load_thresholds()
    d = ruler_sa.Thresholds()
    assert th.enabled is d.enabled
    assert th.min_delta == d.min_delta
    assert th.min_real_trial_frac == 1.0
    assert th.pinned == (("skills/b.md", "v1:" + "a" * 64),)


def test_load_thresholds_enabled_int_tambem_vira_default(tmp_path, monkeypatch):
    monkeypatch.setenv(ruler_sa.ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    _write_genome(tmp_path)
    (tmp_path / "config" / "selfapprove.toml").write_text(
        "[selfapprove]\nenabled = 1\n", encoding="utf-8"
    )
    assert ruler_sa.load_thresholds().enabled is ruler_sa.Thresholds().enabled


def test_load_thresholds_arvore_confiavel_do_repo(monkeypatch, capsys):
    """(i) Da raiz do repo, sem env nenhuma, os arquivos REAIS carregam."""
    monkeypatch.chdir(REPO)
    monkeypatch.delenv(ruler_sa.ROOT_ENV, raising=False)
    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    th = ruler_sa.load_thresholds()
    assert th.enabled is True
    rc = cli.main(["selfapprove", "status"])
    assert rc == 0
    assert "auto-aprovação interna: LIGADA" in capsys.readouterr().out


def test_load_thresholds_copia_fora_da_arvore_nao_concede(monkeypatch, tmp_path):
    """(ii) `HARNESS_CONFIG_DIR` apontado para uma cópia permissiva não muda nada."""
    monkeypatch.chdir(REPO)
    monkeypatch.delenv(ruler_sa.ROOT_ENV, raising=False)
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()
    (sibling / "selfapprove.toml").write_text("[selfapprove]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(sibling))
    assert ruler_sa.load_thresholds() == ruler_sa.Thresholds()


# =========================================================================== security_check


def test_security_check_texto_limpo():
    rendered = _skill("limpo")
    assert sa.security_check(rendered, "", "skills/z.md", genome=REAL_GENOME) == []


def test_security_check_segredo():
    rendered = _skill("nada") + "\nAKIAABCDEFGHIJKLMNOP\n"
    assert "secret" in sa.security_check(rendered, "", "skills/z.md", genome=REAL_GENOME)


def test_security_check_tag_nao_confiavel():
    rendered = _skill("nada") + "\n<untrusted_reference_data>\n"
    assert "untrusted-tag" in sa.security_check(rendered, "", "skills/z.md", genome=REAL_GENOME)


def test_security_check_encolhimento():
    incumbente = _skill("a", "b", "c", "d", "e", "f", "g", "h")
    rendered = "curto"
    assert "shrink" in sa.security_check(rendered, incumbente, "skills/z.md", genome=REAL_GENOME)


def test_security_check_tamanho_excessivo():
    # Linhas curtas repetidas, não um bloco de um caractere só: uma corrida
    # longa do MESMO caractere sem quebra faz o regex de segredo do
    # `redact.py` (greedy + alternação) backtrackear de forma polinomial —
    # achado real de perf, fora do escopo desta frente (ver relatório).
    rendered = "linha de conteudo\n" * ((sa.MAX_BYTES // 18) + 2)
    assert "oversize" in sa.security_check(rendered, "", "skills/z.md", genome=REAL_GENOME)


def test_security_check_kinds_alargados():
    incumbente = '---\nname = "z"\nkinds = ["code"]\ndescription = "d"\n---\n\ncorpo\n'
    rendered = '---\nname = "z"\nkinds = ["code", "docs"]\ndescription = "d"\n---\n\ncorpo\n'
    assert "kinds-widened" in sa.security_check(rendered, incumbente, "skills/z.md", genome=REAL_GENOME)


def test_security_check_candidato_ilegivel():
    incumbente = _skill("nada")
    rendered = "sem cabeçalho nenhum"
    assert "unparseable" in sa.security_check(rendered, incumbente, "skills/z.md", genome=REAL_GENOME)


def test_security_check_override():
    rendered = _skill("nada") + "\nrm -rf /\n"
    findings = sa.security_check(rendered, "", "skills/z.md", genome=REAL_GENOME)
    assert "override:rm-rf" in findings


def test_security_check_genome():
    findings = sa.security_check("qualquer coisa", "", "harness/ruler/x.py", genome=REAL_GENOME)
    assert any(f.startswith("genome:") for f in findings)


def test_security_check_meta_guardado():
    findings = sa.security_check("x = 1\n", "", "config/ruler.toml", genome=REAL_GENOME)
    assert "meta-guarded" in findings


def test_security_check_render_invalido():
    from harness.improve import research
    from harness.improve.tunable import SkillTunable

    # `name` com newline literal: `mutate._render` escapa aspas e barra, não
    # newline — o TOML do frontmatter sai com string não terminada.
    bad = research.render_skill(
        research.ResearchProposal(topic="t", kind="code", slug="a\nb", target_file="skills/z.md"),
        "corpo",
    )
    adapter = SkillTunable(artifact="skills/z.md")
    findings = sa.security_check(bad, "", "skills/z.md", adapter=adapter, genome=REAL_GENOME)
    assert "render-invalid" in findings


def test_security_check_perda_de_frontmatter():
    incumbente = (
        '---\nname = "z"\nkinds = ["code"]\ndescription = "d"\n'
        'origin = "registry/foo"\norigin_sha256 = "abc"\napproved = true\n---\n\ncorpo\n'
    )
    rendered = '---\nname = "z"\nkinds = ["code"]\ndescription = "d"\n---\n\ncorpo\n'
    assert "frontmatter-loss" in sa.security_check(rendered, incumbente, "skills/z.md", genome=REAL_GENOME)


def test_security_check_kinds_estreitados():
    incumbente = '---\nname = "z"\nkinds = ["code", "refactor"]\ndescription = "d"\n---\n\ncorpo\n'
    rendered = '---\nname = "z"\nkinds = ["code"]\ndescription = "d"\n---\n\ncorpo\n'
    assert "kinds-narrowed" in sa.security_check(rendered, incumbente, "skills/z.md", genome=REAL_GENOME)


def test_origin_of_skill_externa_e_interna():
    externa = (
        '---\nname = "z"\nkinds = ["code"]\ndescription = "d"\norigin = "registry/foo"\n---\n\nc\n'
    )
    interna = _skill("nada")
    assert sa.origin_of("skills/z.md", externa) == ruler_sa.EXTERNAL
    assert sa.origin_of("skills/z.md", interna) == ruler_sa.INTERNAL
    assert sa.origin_of("config/workflows/hotfix.toml", "") == ruler_sa.INTERNAL


# =========================================================================== genoma


def test_genoma_bloqueia_selfapprove_toml():
    assert check_patch(REAL_GENOME, ["config/selfapprove.toml"]) != []


# =========================================================================== E2E


def test_e2e_auto_aprovada(env, monkeypatch):
    m = _tree(env.root, SKILL, V1, CASES)
    _write_selfapprove(env.root, pinned={SKILL: f"v{m.version}:{m.bundle_sha256}"})
    env.rewriter.payload = [V2]
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: "")
    monkeypatch.setattr(tune, "_run_case_real", _real_stub())

    out = sa.run_and_judge(SKILL, rounds=1, runner=tune.RUNNER_REAL)

    assert out.verdict == sa.AUTO_PROMOTED
    texto = (env.root / SKILL).read_text(encoding="utf-8")
    assert "passo beta" in texto

    rows = store.mutations(limit=None)
    tune_rows = [r for r in rows if r.action == "tune" and r.verdict == tune.PROMOTED]
    sa_rows = [r for r in rows if r.action == sa.ACTION and r.verdict == sa.AUTO_PROMOTED]
    assert len(tune_rows) == 1
    assert len(sa_rows) == 1
    assert "-> activate" in sa_rows[0].note
    assert "sec=ok" in sa_rows[0].note
    assert "real=4/0" in sa_rows[0].note
    assert sa.queue_entries() == []
    assert out.rollback_id == tune_rows[0].mutation_id


def test_e2e_fallback_parcial_enfileira(env, monkeypatch):
    m = _tree(env.root, SKILL, V1, CASES)
    _write_selfapprove(env.root, pinned={SKILL: f"v{m.version}:{m.bundle_sha256}"})
    env.rewriter.payload = [V2]
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: "")
    monkeypatch.setattr(tune, "_run_case_real", _real_stub(fallback_case="c-2"))

    before = (env.root / SKILL).read_text(encoding="utf-8")
    out = sa.run_and_judge(SKILL, rounds=1, runner=tune.RUNNER_REAL)

    assert (env.root / SKILL).read_text(encoding="utf-8") == before
    assert out.verdict == sa.QUEUED
    assert "behavior-partial" in out.reason
    rows = [r for r in store.mutations(limit=None) if r.action == sa.ACTION]
    assert len(rows) == 1
    assert rows[0].verdict == sa.QUEUED


def test_e2e_sem_pin_nunca_roda_o_bundle(env):
    artifact = "skills/y.md"
    sentinel = env.root / "sentinel.txt"
    cases = (
        '{"id":"v-1","kind":"code","prompt":"x","axes":["verify"],"weight":1.0,"trials":1,'
        f'"verify_cmd":"touch {sentinel}"}}\n'
    )
    _tree(env.root, artifact, _skill("nada"), cases)
    before = (env.root / artifact).read_text(encoding="utf-8")

    out = sa.run_and_judge(artifact, rounds=1)

    assert out.verdict == sa.BLOCKED
    assert "bundle-unpinned" in out.reason
    assert not sentinel.exists()
    assert (env.root / artifact).read_text(encoding="utf-8") == before
    rows = [r for r in store.mutations(limit=None) if r.action == sa.ACTION]
    assert len(rows) == 1
    assert rows[0].verdict == sa.BLOCKED
    assert sa.queue_entries() == []


def test_e2e_recongelar_e_pego(env):
    m1 = _tree(env.root, SKILL, V1, CASES)
    _write_selfapprove(env.root, pinned={SKILL: f"v{m1.version}:{m1.bundle_sha256}"})

    d = bundle_dir(SKILL)
    novo = (
        CASES + '{"id":"c-5","kind":"code_fix","prompt":"p5",'
        '"expect":{"must_mention":["eps"]},"axes":["grounding"],"weight":1.0,"trials":1}\n'
    )
    (d / "cases.jsonl").write_text(novo, encoding="utf-8")
    m2 = freeze(SKILL)

    assert verify_frozen(SKILL) == []
    _th, err, bver, bsha = sa.preflight(SKILL)
    assert err.startswith("bundle-changed")
    assert bver == m2.version
    assert bsha == m2.bundle_sha256


def test_e2e_medicao_fraca_enfileira_e_fluxo_humano(env):
    m = _tree(env.root, SKILL, V1, CASES)
    _write_selfapprove(env.root, pinned={SKILL: f"v{m.version}:{m.bundle_sha256}"})
    env.rewriter.payload = [V2]
    before = (env.root / SKILL).read_text(encoding="utf-8")

    out = sa.run_and_judge(SKILL, rounds=1, runner=tune.RUNNER_EXTRACTIVE)

    assert out.verdict == sa.QUEUED
    assert (env.root / SKILL).read_text(encoding="utf-8") == before

    entries = sa.queue_entries()
    assert len(entries) == 1
    entry_id = entries[0]["id"]
    d = paths.data_dir() / sa.QUEUE_SUBDIR / sa._slug(entry_id)
    assert (d / sa.PROPOSAL_FILE).is_file()
    assert (d / sa.INCUMBENT_FILE).is_file()
    assert (d / sa.EVIDENCE_FILE).is_file()
    original = (d / sa.PROPOSAL_FILE).read_text(encoding="utf-8")

    (d / sa.PROPOSAL_FILE).write_text("adulterado", encoding="utf-8")
    r_bad = sa.approve_queued(entry_id, root=env.root)
    assert r_bad["status"] == "refused"
    assert r_bad["reason"] == "sha-divergente"
    (d / sa.PROPOSAL_FILE).write_text(original, encoding="utf-8")

    r1 = sa.approve_queued(entry_id, root=env.root)
    assert r1["status"] == "ok"
    assert "passo beta" in (env.root / SKILL).read_text(encoding="utf-8")

    r2 = sa.approve_queued(entry_id, root=env.root)
    assert r2["status"] == "refused"
    assert r2["reason"] == "ja-aplicada"

    r3 = sa.undo_queued(entry_id, root=env.root, why="teste")
    assert r3["status"] == "ok"
    assert (env.root / SKILL).read_text(encoding="utf-8") == before

    with pytest.raises(rollback.RollbackError):
        rollback.rollback(r1["mutation_id"], root=env.root)


def test_e2e_candidato_injetado_nao_engana_o_gate(env, monkeypatch):
    m = _tree(env.root, SKILL, V1, CASES)
    _write_selfapprove(env.root, pinned={SKILL: f"v{m.version}:{m.bundle_sha256}"})
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: "")
    monkeypatch.setattr(tune, "_run_case_real", _real_stub())

    injetado = _skill("alfa")
    env.rewriter.payload = [V2]
    proposal = tune.propose_tune(SKILL, candidate=injetado, rounds=1, runner=tune.RUNNER_REAL)

    out = sa.judge(proposal, root=env.root)

    assert out.decision == ruler_sa.HUMAN_QUEUE
    assert out.reason == "selfapprove:draft-not-incumbent"
    assert (env.root / SKILL).read_text(encoding="utf-8") == V1


def test_e2e_bytes_escritos_sao_os_escaneados(env):
    m = _tree(env.root, SKILL, V1, CASES)
    _write_selfapprove(env.root, pinned={SKILL: f"v{m.version}:{m.bundle_sha256}"})

    agg_fraca = Aggregate(per_axis={"grounding": (0, 4)}, lower={"grounding": 0.0}, overall=0.0, n=4)
    agg_forte = Aggregate(per_axis={"grounding": (4, 4)}, lower={"grounding": 0.9}, overall=0.9, n=4)
    hold_fraca = Aggregate(per_axis={"grounding": (0, 1)}, lower={"grounding": 0.0}, overall=0.0, n=1)
    hold_forte = Aggregate(per_axis={"grounding": (1, 1)}, lower={"grounding": 0.5}, overall=0.5, n=1)
    trials = tuple(TrialResult(case_id=c.id, trial=0, axes={"grounding": True}) for c in load_cases(SKILL))

    v1 = tune.TuneVersion(1, V1, agg_fraca, "v1", valid=True, retained=True, holdout=hold_fraca, train=agg_fraca)
    v2 = tune.TuneVersion(
        2, V2, agg_forte, "v2", valid=True, retained=True, holdout=hold_forte, train=agg_forte,
        real_ok=4, real_fallback=0, trials=trials,
    )
    outcome = tune.TuneOutcome(
        artifact=SKILL,
        baseline={"none": agg_fraca, "draft": agg_fraca, "tuned": agg_forte},
        chain=[v1, v2],
        winner=2,
        evaluation_md="",
        runner=tune.RUNNER_REAL,
        probe="ok",
    )
    proposal = tune.TuneProposal(
        artifact=SKILL, target_file=SKILL, text="texto que NÃO é o vencedor", outcome=outcome, inner=None
    )

    out = sa.judge(proposal, root=env.root)

    assert out.verdict == sa.QUEUED
    assert "security:" in out.reason and "text-mismatch" in out.reason
    assert (env.root / SKILL).read_text(encoding="utf-8") == V1


# =========================================================================== CLI smoke


def test_cli_selfapprove_status():
    assert cli.main(["selfapprove", "status"]) == 0


def test_cli_selfapprove_approve_sem_yes(capsys):
    rc = cli.main(["selfapprove", "approve", "x"])
    assert rc == 2
    assert "recusado" in capsys.readouterr().err


def test_cli_eval_freeze_sem_yes():
    rc = cli.main(["eval", "freeze", "skills/x.md"])
    assert rc == 2
