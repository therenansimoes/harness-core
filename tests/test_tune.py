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
from harness.evals.score import RULER_VERSION, aggregate, score_trial
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
    # só não foi retida — o motivo cita o placar dos dois lados E o delta por
    # eixo, que é o que a próxima tentativa consegue atacar.
    assert out.chain[2].valid is True
    assert out.chain[2].retained is False
    assert out.chain[2].agg.n == 1
    assert out.chain[2].reason == (
        f"v3 descartada: train {out.chain[2].train.overall:.3f} "
        f"<= {out.chain[1].train.overall:.3f}; "
        "PIOROU coverage 0.207->0.000, grounding 0.207->0.000; "
        "recupere coverage, grounding."
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
    # Frontmatter presente e TOML torto: ilegibilidade que o loop NÃO sabe
    # consertar sozinho (corpo sem cabeçalho ele reanexa — outro teste).
    env.rewriter.payload = ["---\nname = sem aspas\n---\n\n# corpo\n"]

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


# --------------------------------------------------------------------------- frontmatter


def test_frontmatter_reanexado_salva_a_rodada(env):
    """O modo de falha nº 1 do 9B local: volta o corpo, come o cabeçalho.

    Antes, isso era `skill ilegível` e uma rodada inteira jogada fora. Agora o
    cabeçalho do incumbente é reanexado por cópia e a candidata é MEDIDA — que é
    o único jeito de descobrir se ela era boa.
    """
    _skill_tree(env)
    env.rewriter.payload = ["# guia\n- passo alfa\n- passo beta\n"]

    out = tune.run_tune(SKILL, rounds=1, trials=1)

    v2 = out.chain[1]
    assert v2.valid is True
    assert v2.text.startswith('---\nname = "x"')
    assert "- passo beta" in v2.text
    assert tune.FRONTMATTER_NOTE.strip() in v2.reason
    # Medida de verdade, e não só validada: a normalização devolve a rodada ao
    # loop, não só ao validate.
    assert v2.agg.n == 1
    assert out.winner == 2


def test_frontmatter_nao_e_reanexado_em_eco_do_prompt(env):
    """Candidato que já carrega o cabeçalho (eco do mock, cerca de código) não
    ganha um segundo: dois cabeçalhos passariam no validate e viraria promoção
    de lixo."""
    assert tune._restore_frontmatter(V1, "eco:\n" + V1) == ("eco:\n" + V1, False)
    assert tune._restore_frontmatter(V1, V2) == (V2, False)
    # Original sem cabeçalho (workflow em TOML) nunca dispara.
    assert tune._restore_frontmatter("[nodes]\n", "corpo\n") == ("corpo\n", False)

    texto, fixed = tune._restore_frontmatter(V1, "# guia\n- passo gama\n")
    assert fixed is True
    assert texto.endswith("# guia\n- passo gama\n")


# --------------------------------------------------------------------------- runner real


class _FakeChat:
    """Backend que responde no CHAT, que é como a resposta de um caso volta:
    trace com mensagens `ai`, sem `result` e sem arquivo."""

    def __init__(self, resposta: str = "resposta com alfa e beta", fail: bool = False) -> None:
        self.resposta, self.fail, self.reqs = resposta, fail, []

    def preflight(self):
        return SimpleNamespace(ok=True, reason="fake")

    def execute(self, req):
        self.reqs.append(req)
        if self.fail:
            return _exec_result(req, ok=False, exit_reason="timeout")
        req.trace_path.parent.mkdir(parents=True, exist_ok=True)
        req.trace_path.write_text(
            json.dumps({"type": "human", "content": req.prompt}, ensure_ascii=False)
            + "\n"
            + json.dumps({"type": "ai", "content": self.resposta}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        return _exec_result(req, ok=True, exit_reason="done")


def test_runner_real_julga_a_resposta_do_modelo(monkeypatch):
    fake = _FakeChat()
    _route(monkeypatch, fake)
    case = EvalCase(id="s-1", kind="code_fix", prompt="como corrigir o import")

    out = tune._run_case_real(V1, case, model=None, max_usd=1.0)

    assert out == "resposta com alfa e beta"
    (req,) = fake.reqs
    # O artefato entra como ORIENTAÇÃO e o caso como pedido — a saída julgada
    # deixa de ser o próprio artefato, que é o ponto do runner real.
    assert "--- orientação ---" in req.prompt
    assert "como corrigir o import" in req.prompt
    assert req.timeout_s == tune.RUN_TIMEOUT_S
    # >1 porque `run_limit=1` faz o middleware ocupar a última fala com o aviso
    # de teto e a resposta do caso nunca chega.
    assert req.max_turns == tune.RUN_MAX_TURNS > 1


def test_runner_real_com_timeout_cai_no_extrativo(monkeypatch):
    fake = _FakeChat(fail=True)
    _route(monkeypatch, fake)
    case = EvalCase(id="s-1", kind="code_fix", prompt="p")

    # Fallback e não exceção: tune noturno não trava por servidor lento.
    assert tune._run_case_real(V1, case, model=None, max_usd=1.0) == V1
    assert len(fake.reqs) == 1


def test_runner_real_sem_lm_studio_cai_no_extrativo(monkeypatch):
    _no_lmstudio(monkeypatch)
    case = EvalCase(id="s-1", kind="code_fix", prompt="p")

    # Nem chega a chamar backend: a sonda de zero token já respondeu.
    assert tune._run_case_real(V1, case, model=None, max_usd=1.0) == V1


def test_runner_real_e_carimbado_na_cadeia(env, monkeypatch):
    _skill_tree(env)
    chamadas = []

    def real(text, case, *, model=None, max_usd=0.0):
        chamadas.append(case.id)
        return "resposta do modelo com alfa e beta"

    monkeypatch.setattr(tune, "_run_case_real", real)
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: "")

    out = tune.run_tune(SKILL, rounds=0, trials=1, runner=tune.RUNNER_REAL)

    assert (out.runner, out.probe) == (tune.RUNNER_REAL, "ok")

    # O extrativo (o stub do fixture) não foi chamado NENHUMA vez: runner é
    # escolhido uma vez e vale para baseline e cadeia inteira.
    assert env.runner.calls == []
    assert chamadas == ["s-1", "s-1"]
    # A resposta do modelo é que foi julgada, e ela contém as duas âncoras.
    assert out.winning.agg.overall > 0
    rows = json.loads((tune.chain_dir(SKILL) / "chain.json").read_text(encoding="utf-8"))
    assert [r["runner"] for r in rows] == [tune.RUNNER_REAL]
    md = (tune.chain_dir(SKILL) / "EVALUATION.md").read_text(encoding="utf-8")
    assert f"Runner: `{tune.RUNNER_REAL}`" in md.splitlines()[2]


def test_probe_reprova_e_o_run_inteiro_vira_extrativo(env, monkeypatch):
    """Probe pré-cadeia: uma falha decide o run TODO, não caso a caso.

    Antes disto cada caso pagava o `RUN_TIMEOUT_S` inteiro antes de cair no
    extrativo sozinho — cadeia heterogênea e ~28 x 60s de tempo morto.
    """
    _skill_tree(env)
    reais = []
    monkeypatch.setattr(tune, "_run_case_real", lambda *a, **k: reais.append(1) or "x")
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: "TuneError: timeout")

    out = tune.run_tune(SKILL, rounds=0, trials=1, runner=tune.RUNNER_REAL)

    # ZERO chamada real depois do probe: o fallback é do run, não do caso.
    assert reais == []
    assert env.runner.calls == ["s-1", "s-1"]
    assert (out.runner, out.probe) == (tune.RUNNER_PROBE_FALLBACK, "TuneError: timeout")
    rows = json.loads((tune.chain_dir(SKILL) / "chain.json").read_text(encoding="utf-8"))
    assert [r["runner"] for r in rows] == [tune.RUNNER_PROBE_FALLBACK]
    md = (tune.chain_dir(SKILL) / "EVALUATION.md").read_text(encoding="utf-8")
    assert f"Runner: `{tune.RUNNER_PROBE_FALLBACK}`" in md.splitlines()[2]


def test_probe_roda_uma_vez_no_primeiro_caso_de_treino(env, monkeypatch):
    """Um probe, no caso de treino — nunca no holdout, nunca um por caso."""
    _skill_tree(env)
    vistos = []

    def probe(text, case, *, model=None, max_usd=0.0):
        vistos.append((case.id, text))
        return ""

    monkeypatch.setattr(tune, "_probe_real", probe)
    monkeypatch.setattr(tune, "_run_case_real", lambda text, case, **k: "alfa e beta")

    tune.run_tune(SKILL, rounds=0, trials=1, runner=tune.RUNNER_REAL)

    assert [c for c, _ in vistos] == ["s-1"]
    # O artefato vai junto: probe sem orientação não exercita o mesmo caminho.
    assert vistos[0][1] == V1


def test_probe_nao_roda_no_runner_extrativo(env, monkeypatch):
    """Extrativo não depende de servidor: sondar seria gastar rede à toa."""
    _skill_tree(env)
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: pytest.fail("probe no extrativo"))

    out = tune.run_tune(SKILL, rounds=0, trials=1)

    assert (out.runner, out.probe) == (tune.RUNNER_EXTRACTIVE, "")


def test_probe_sem_lm_studio_reprova_sem_chamar_backend(monkeypatch):
    """A sonda de zero token já responde: nem chega a abrir um run."""
    _no_lmstudio(monkeypatch)
    case = EvalCase(id="s-1", kind="code_fix", prompt="p")

    assert "indisponível" in tune._probe_real(V1, case, model=None, max_usd=1.0)


def test_probe_reprova_com_resposta_vazia(monkeypatch):
    """Thinking que não vira texto é reprovação, não aprovação silenciosa."""
    _route(monkeypatch, _FakeChat(resposta="   "))
    case = EvalCase(id="s-1", kind="code_fix", prompt="p")

    # `_call` já levanta em texto vazio; a guarda extra é para backend que
    # devolva só espaço em branco.
    assert tune._probe_real(V1, case, model=None, max_usd=1.0) != ""


def test_cli_diz_o_que_o_probe_decidiu(env, monkeypatch, capsys):
    """`harness tune --runner real` que caiu no extrativo tem que DIZER isso —
    senão a única pista fica dentro do chain.json."""
    from harness import cli

    _skill_tree(env)
    monkeypatch.setattr(tune, "_probe_real", lambda *a, **k: "TuneError: timeout")
    args = SimpleNamespace(
        artifact=SKILL, rounds=0, model=tune.DEFAULT_MODEL, runner=tune.RUNNER_REAL
    )

    assert cli.cmd_tune(args) == 0

    saida = capsys.readouterr().out
    assert "probe: TuneError: timeout" in saida
    assert f"(runner={tune.RUNNER_PROBE_FALLBACK})" in saida


def test_probe_aprova_quando_o_modelo_responde(monkeypatch):
    fake = _FakeChat()
    _route(monkeypatch, fake)
    case = EvalCase(id="s-1", kind="code_fix", prompt="p")

    assert tune._probe_real(V1, case, model=None, max_usd=1.0) == ""
    # UMA chamada: o probe é uma pergunta, não uma medição.
    assert len(fake.reqs) == 1


def test_runner_desconhecido_para_antes_de_medir(env):
    _skill_tree(env)

    with pytest.raises(ValueError, match="runner desconhecido"):
        tune.run_tune(SKILL, rounds=0, trials=1, runner="chute")

    assert env.runner.calls == []


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


# O gate julga TREINO. Três casos com baldes conhecidos: s-1 (balde 0) é o
# holdout e reprova sempre ("gama" não aparece em versão nenhuma); s-2 e s-3 são
# treino. O peso 3 do holdout dilui o ganho de `coverage` no bundle inteiro e a
# troca de `grounding` custa cheio ali — resultado: o treino sobe, o holdout não
# se mexe e o overall do bundle CAI. Sob o gate antigo (bundle) a candidata era
# descartada; sob o gate de treino ela é retida.
TRAIN_GATE_CASES = (
    '{"id":"s-1","kind":"code_fix","prompt":"holdout imexível",'
    '"expect":{"must_mention":["gama"]},"axes":["coverage"],"weight":3.0,"trials":1}\n'
    '{"id":"s-2","kind":"code_fix","prompt":"treino coverage",'
    '"expect":{"must_mention":["alfa"]},"axes":["coverage"],"weight":1.0,"trials":2}\n'
    '{"id":"s-3","kind":"code_fix","prompt":"treino grounding",'
    '"expect":{"must_mention":["beta"]},"axes":["grounding"],"weight":1.0,"trials":1}\n'
)


def test_gate_julga_treino_nao_o_bundle(env):
    _tree(env.root, SKILL, _skill("beta"), TRAIN_GATE_CASES)
    env.rewriter.payload = [_skill("alfa")]

    out = tune.run_tune(SKILL, rounds=1)

    v1, v2 = out.chain
    assert v2.train.overall > v1.train.overall
    assert v2.holdout.overall == v1.holdout.overall  # holdout intocado: não veta
    # A manchete do bundle piora, e ainda assim a candidata entra: o reescritor
    # só vê treino, então é o treino que decide se ele acertou.
    assert v2.agg.overall < v1.agg.overall
    assert v2.retained is True
    assert out.winner == 2

    rows = json.loads((tune.chain_dir(SKILL) / "chain.json").read_text(encoding="utf-8"))
    assert [r["retained"] for r in rows] == [True, True]
    assert rows[1]["train_overall"] == v2.train.overall


def test_trials_jsonl_grava_diagnostico_e_saida(env):
    _tree(env.root, SKILL, _skill("beta"), TRAIN_GATE_CASES)

    tune.run_tune(SKILL, rounds=0)

    linhas = [
        json.loads(ln)
        for ln in (tune.chain_dir(SKILL) / "v1.trials.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    # 1 (s-1) + 2 (s-2) + 1 (s-3) trials, na ordem do bundle.
    assert [r["case_id"] for r in linhas] == ["s-1", "s-2", "s-2", "s-3"]
    assert linhas[0]["axes"] == {"coverage": False}
    assert linhas[0]["notes"] == {"coverage": 'faltou conter LITERALMENTE: "gama"'}
    assert "passo beta" in linhas[0]["output"]
    # Eixo que passou não gera nota — o arquivo é diagnóstico, não log.
    assert linhas[3]["axes"] == {"grounding": True} and linhas[3]["notes"] == {}


def test_evaluation_md_carimba_a_regua(env):
    _skill_tree(env)

    tune.run_tune(SKILL, rounds=0, trials=1)

    md = (tune.chain_dir(SKILL) / "EVALUATION.md").read_text(encoding="utf-8")
    rows = json.loads((tune.chain_dir(SKILL) / "chain.json").read_text(encoding="utf-8"))
    assert f"`ruler_version={RULER_VERSION}`" in md.splitlines()[2]
    assert rows[0]["ruler_version"] == RULER_VERSION


def test_rewrite_prompt_cita_ancora_literal_do_caso_real(env):
    """O prompt de reescrita carrega o DADO (as strings que faltaram), não o rótulo."""
    artifact = "skills/python-fixes.md"
    original = (REPO / artifact).read_text(encoding="utf-8")
    _tree(
        env.root,
        artifact,
        original,
        (REPO / "evals/skills/python-fixes/cases.jsonl").read_text(encoding="utf-8"),
    )
    adapter = tunable_for(artifact)
    pf2 = next(c for c in load_cases(artifact) if c.id == "pf-002")
    weak = [score_trial(pf2, adapter.produce(pf2, original))]
    assert weak[0].axes["coverage"] is False

    prompt = adapter.rewrite_prompt(original, weak, cases=[pf2], agg=aggregate(weak))

    assert 'faltou conter LITERALMENTE: "frozen=True", "dataclasses.replace"' in prompt
    assert prompt.count("pf-002") == 2  # o caso no bloco (i) e a reprovação no (ii)
    assert "coverage: faltou conter LITERALMENTE" in prompt
    # Bloco (i): o que é pontuado é a RESPOSTA, não o texto escrito.
    assert "O texto que você escreve NÃO é pontuado" in prompt
    # Bloco (iii): placar por eixo e o aviso de não regredir.
    assert "- coverage 0/1 (0.000)" in prompt
    assert "EIXOS JÁ EM 100%" in prompt


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
