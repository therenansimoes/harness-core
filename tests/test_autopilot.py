"""autopilot_graph: o ciclo inteiro em sandbox, com mock — zero LLM, zero rede.

Todo teste aqui roda numa árvore de tmp (config + data próprios) e com backend
registrado à mão. O que muda entre o braço A e o braço B é UM knob de
`config/models.toml`, e o backend de teste lê esse knob na hora de executar —
é assim que a mutação vira diferença mensurável sem nenhum modelo envolvido.
"""

import os
import shutil
from pathlib import Path

import pytest

from harness.backends import registry
from harness.graph.autopilot_graph import run_autopilot
from harness.improve import escalate, mutate
from harness.ledger import store
from harness.types import Capabilities, ExecResult, Preflight, RunRow

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
UNIT = Path(__file__).parent / "fixtures" / "echo"
OUTPUT = "mock_output.txt"
KNOB = "router.prior_floor"

CATALOG = """
[improve]
sec_cost_usd = 0.0001
n_per_arm = 6

[[rule]]
id = "floor_up"
target_file = "config/models.toml"
key = "router.prior_floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: piso mais alto escala mais cedo"
"""

CATALOG_SELADO = """
[improve]
n_per_arm = 6

[[rule]]
id = "mexe_no_exame"
target_file = "benchmarks/sealed/exam.toml"
key = "exam.floor"
from = 0.50
to = 0.65
fails_on = ["verify_failed"]
hypothesis = "sintética: proibida — exame selado"
"""


class KnobBackend:
    """Sucesso condicionado a um knob do config: o braço é a config, não o modelo.

    Escreve o arquivo que o `verify` da unit `echo` procura só quando
    `router.prior_floor` vale `writes_on`. Determinístico e sem I/O de rede.
    """

    name = "knob"

    def __init__(self, cfg_path: Path, writes_on: float) -> None:
        self.cfg_path = cfg_path
        self.writes_on = writes_on

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req) -> ExecResult:
        try:
            value = mutate.read_value(self.cfg_path, KNOB)
        except mutate.MutationError:
            value = None
        changed: tuple[str, ...] = ()
        if value == self.writes_on:
            req.workspace.mkdir(parents=True, exist_ok=True)
            (req.workspace / OUTPUT).write_text("x", encoding="utf-8")
            changed = (OUTPUT,)
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=changed,
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Raiz isolada: `config/` real do repo, catálogo sintético, data própria."""
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "config" / "catalog.toml").write_text(CATALOG, encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def knob(sandbox):
    """Registra `knob_b` (passa com a mutação) e `knob_a` (passa sem ela)."""
    cfg = sandbox / "config" / "models.toml"
    registry.register("knob_b", lambda: KnobBackend(cfg, 0.65))
    registry.register("knob_a", lambda: KnobBackend(cfg, 0.50))
    try:
        yield
    finally:
        registry.unregister("knob_b")
        registry.unregister("knob_a")


def db(sandbox: Path) -> Path:
    return sandbox / "data" / store.DB_NAME


def seed_failures(sandbox: Path, n: int = 3) -> None:
    """Gradiente: sem falha no ledger, `pick_target` não tem o que atacar."""
    for i in range(n):
        store.record_run(
            RunRow(
                run_id=f"seed{i}",
                unit_id="echo",
                project=None,
                backend="mock",
                model=None,
                tier="t0",
                kind="code",
                ok=False,
                exit_reason="verify_failed",
                sec_total=10.0,
                sec_provision=0.0,
                cost_usd=0.0,
                intervention=False,
                created_at=store.now_iso(),
            ),
            path=db(sandbox),
        )


def snapshot(root: Path) -> dict[str, bytes]:
    """Árvore inteira em memória: nome -> conteúdo. É o detector de escrita
    fora do lugar (e de config que não voltou byte-idêntica)."""
    out: dict[str, bytes] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def autopilot(sandbox: Path, **kw):
    return run_autopilot(sandbox / "data", units=[UNIT], root=sandbox, **kw)


# --- veredito -------------------------------------------------------------------


def test_autopilot_keep(sandbox, knob):
    """Mutação que faz o braço B passar e o A falhar: KEEP e a config FICA."""
    seed_failures(sandbox)
    alvo = sandbox / "config" / "models.toml"

    report = autopilot(sandbox, backend="knob_b")

    assert report.escalation is None
    assert len(report.results) == 1
    r = report.results[0]
    assert (r["rule_id"], r["verdict"]) == ("floor_up", "KEEP")
    assert (r["arm_a"], r["arm_b"]) == ("0/6", "6/6")
    assert r["delta"] == pytest.approx(1.0)
    assert r["reverted"] is False
    # a mudança sobreviveu: é o único caso em que o toml não volta
    assert mutate.read_value(alvo, KNOB) == 0.65

    linha = store.mutations(path=db(sandbox))[0]
    assert (linha.verdict, linha.reverted, linha.rule_id) == ("KEEP", False, "floor_up")
    assert linha.mutation_id == r["mutation_id"]


def test_autopilot_discard_reverte_byte_identico(sandbox, knob):
    """Braço A ganha: DISCARD e o toml volta exatamente como estava."""
    seed_failures(sandbox)
    alvo = sandbox / "config" / "models.toml"
    antes = alvo.read_bytes()

    report = autopilot(sandbox, backend="knob_a")

    r = report.results[0]
    assert (r["verdict"], r["arm_a"], r["arm_b"]) == ("DISCARD", "6/6", "0/6")
    assert r["reverted"] is True
    assert alvo.read_bytes() == antes
    assert store.mutations(path=db(sandbox))[0].reverted is True


def test_autopilot_inconclusive_reverte(sandbox, knob):
    """Backend que ignora a config: os braços empatam e a régua não promove."""
    seed_failures(sandbox)
    antes = (sandbox / "config" / "models.toml").read_bytes()

    report = autopilot(sandbox, backend="mock")

    r = report.results[0]
    assert (r["verdict"], r["arm_a"], r["arm_b"]) == ("INCONCLUSIVE", "6/6", "6/6")
    assert (sandbox / "config" / "models.toml").read_bytes() == antes


# --- escalação ------------------------------------------------------------------


def test_autopilot_interrupt_sem_gradiente(sandbox, knob):
    """Histórico vazio: nada a atacar, o loop para e chama o humano."""
    antes = snapshot(sandbox / "config")

    report = autopilot(sandbox, backend="knob_b")

    assert report.escalation is not None
    assert report.escalation["reason"] == escalate.NO_GRADIENT
    assert report.escalation["evidence"]["history"] == 0
    assert report.escalation["unit"] == [str(UNIT)]
    assert report.results == ()
    assert report.cycles == 0
    assert snapshot(sandbox / "config") == antes  # não escreveu nada
    assert store.mutations(path=db(sandbox)) == []


def test_autopilot_interrupt_deadline(sandbox, knob):
    """Deadline já estourado na entrada do primeiro nó: escala, não roda."""
    seed_failures(sandbox)

    report = autopilot(sandbox, backend="knob_b", deadline_s=-1)

    assert report.escalation["reason"] == escalate.DEADLINE
    assert report.escalation["evidence"]["node"] == "pick_target"
    assert store.history(path=db(sandbox), limit=100) == store.history(
        path=db(sandbox), limit=3
    )  # nenhuma run nova além das 3 sementes


def test_autopilot_escala_violacao_de_genoma(sandbox, knob):
    """Regra apontada pro exame selado: rejeitada, registrada, escalada."""
    seed_failures(sandbox)
    (sandbox / "config" / "catalog.toml").write_text(CATALOG_SELADO, encoding="utf-8")
    selado = sandbox / "benchmarks" / "sealed" / "exam.toml"
    selado.parent.mkdir(parents=True)
    selado.write_text("[exam]\nfloor = 0.50\n", encoding="utf-8")

    report = autopilot(sandbox, backend="knob_b")

    assert report.escalation["reason"] == escalate.GENOME_VIOLATION
    assert report.escalation["evidence"]["rule"] == "mexe_no_exame"
    assert selado.read_text() == "[exam]\nfloor = 0.50\n"  # nada foi escrito

    linha = store.mutations(path=db(sandbox))[0]
    assert linha.verdict == "REJECTED"
    assert linha.note == "genome:immutable:benchmarks/sealed/exam.toml"
    assert report.results[0]["verdict"] == "REJECTED"


def test_autopilot_resume_do_humano_marca_intervention(sandbox, knob):
    """O humano responde `continue` com a regra na mão: o ciclo roda e as runs
    saem marcadas como intervenção — é o que faz `intervention_rate` subir."""
    report = autopilot(sandbox, backend="knob_b")
    assert report.escalation["reason"] == escalate.NO_GRADIENT

    retomado = autopilot(
        sandbox,
        backend="knob_b",
        thread_id=report.thread_id,
        resume={"action": escalate.CONTINUE, "rule_id": "floor_up"},
    )

    assert retomado.interventions == 1
    assert retomado.results[0]["verdict"] == "KEEP"
    assert retomado.intervention_rate == pytest.approx(1.0)
    assert all(r.intervention for r in store.history(path=db(sandbox), limit=100))


def test_autopilot_resume_abort_encerra_limpo(sandbox, knob):
    """Resposta que não é `continue` encerra o loop sem tocar em nada."""
    antes = snapshot(sandbox / "config")
    report = autopilot(sandbox, backend="knob_b")

    fim = autopilot(
        sandbox,
        backend="knob_b",
        thread_id=report.thread_id,
        resume={"action": escalate.ABORT},
    )

    assert fim.results == ()
    assert fim.interventions == 1
    assert snapshot(sandbox / "config") == antes
    assert store.history(path=db(sandbox), limit=100) == []


def test_autopilot_erro_no_experimento_nao_deixa_config_suja(sandbox, monkeypatch):
    """Backend explodindo no meio do A/B: escala, e o abort devolve o toml.

    É o caminho que garante que uma falha depois do `apply` não deixa o repo
    calibrado com uma mutação que ninguém mediu.
    """
    seed_failures(sandbox)
    alvo = sandbox / "config" / "models.toml"
    antes = alvo.read_bytes()

    class Explode(KnobBackend):
        def execute(self, req):
            raise RuntimeError("backend morreu no meio")

    registry.register("boom", lambda: Explode(alvo, 0.65))
    try:
        report = autopilot(sandbox, backend="boom")
        assert report.escalation["reason"] == escalate.ERROR
        assert "backend morreu" in report.escalation["evidence"]["error"]
        # A explosão foi na 1ª run do braço A, com a mutação já desligada pelo
        # `before_run` — o revert do abort tem que ser idempotente, não estourar.
        fim = autopilot(
            sandbox,
            backend="boom",
            thread_id=report.thread_id,
            resume={"action": escalate.ABORT},
        )
    finally:
        registry.unregister("boom")

    assert alvo.read_bytes() == antes
    linha = store.mutations(path=db(sandbox))[0]
    # ABORTED, não INCONCLUSIVE: experimento sem amostra não é empate.
    assert (linha.verdict, linha.reverted, linha.note) == ("ABORTED", True, "error")
    assert fim.results[0]["arm_a"] == "0/0"  # experimento sem amostra nenhuma


def test_autopilot_exige_unidade(sandbox):
    with pytest.raises(ValueError, match="sem unidade"):
        run_autopilot(sandbox / "data", units=[], root=sandbox)


# --- aceite do PR ----------------------------------------------------------------


def test_autopilot_smoke_5runs(sandbox, knob):
    """Versão barata do "20min sem intervenção": um ciclo inteiro, sozinho.

    Cobre as quatro promessas do aceite do PR-9 — runs no ledger, mutação
    avaliada com veredito, nenhuma escrita fora de `data/`, e
    `intervention_rate` no relatório.
    """
    seed_failures(sandbox)
    antes = snapshot(sandbox)

    report = autopilot(sandbox, cycles=1, deadline_s=300, backend="knob_b")

    runs = [r for r in store.history(path=db(sandbox), limit=100) if r.run_id[:4] != "seed"]
    assert len(runs) >= 5
    assert len(report.results) == 1 and report.results[0]["verdict"] in (
        "KEEP",
        "DISCARD",
        "INCONCLUSIVE",
    )
    assert report.intervention_rate == 0.0 and report.interventions == 0
    assert report.runs_window == len(runs) + 3

    depois = snapshot(sandbox)
    novos = set(depois) - set(antes)
    mudados = {k for k in set(depois) & set(antes) if depois[k] != antes[k]}
    assert all(p.startswith("data/") for p in novos), novos
    # o único arquivo pré-existente que pode mudar é o toml mutado (KEEP)
    assert mudados <= {"config/models.toml", f"data/{store.DB_NAME}"}


def test_cli_improve(sandbox, knob, capsys):
    """`harness improve` imprime veredito por ciclo e a taxa de intervenção."""
    from harness import cli

    seed_failures(sandbox)
    rc = cli.main(
        [
            "improve",
            "--cycles",
            "1",
            "--deadline-s",
            "300",
            "--unit",
            str(UNIT),
            "--backend",
            "knob_b",
        ]
    )

    assert rc == 0
    linhas = capsys.readouterr().out.strip().splitlines()
    assert linhas[0].startswith("ciclo0 floor_up router.prior_floor 0.50->0.65 KEEP")
    assert "a=0/6 b=6/6" in linhas[0] and "mantida" in linhas[0]
    assert linhas[-1].startswith(
        "improve ciclos=1 mutações=1 intervenções=0 intervention_rate=0.00"
    )


def test_cli_improve_sem_unidade_falha_claro(sandbox, capsys, monkeypatch):
    from harness import cli

    monkeypatch.chdir(sandbox)  # sem benchmarks/held_in aqui
    assert cli.main(["improve"]) == 1
    assert "unidade de avaliação" in capsys.readouterr().err


def test_cli_improve_sem_catalogo_falha_claro(sandbox, capsys):
    from harness import cli

    (sandbox / "config" / "catalog.toml").unlink()

    assert cli.main(["improve", "--unit", str(UNIT)]) == 1
    assert "catalog.toml ilegível" in capsys.readouterr().err
