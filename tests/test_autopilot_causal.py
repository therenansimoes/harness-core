"""O canal causal do loop: mutação em `config/*.toml` TEM que chegar na run.

Antes deste PR o `fanout_ab` montava UMA `ArmSpec` na entrada do nó e a passava
para os dois braços, e `run_once` nunca lia config: mutar `models.toml` não
mudava nada em nenhuma run, os braços eram idênticos por construção e o
veredito era INCONCLUSIVE qualquer que fosse a mutação — o loop "media" e não
media nada.

Cada teste aqui prende uma ponta do fio: do toml, pelo router, até o
`ExecRequest` que o backend recebe. O backend é um espião registrado à mão que
guarda cada request — zero modelo, zero rede.
"""

import os
import re
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
KNOB = "tier[0].max_turns"

# UM tier só, de propósito: o experimento é sobre `max_turns` chegar na run, e
# com dois tiers um prior_bump no meio trocaria o braço por motivo alheio à
# mutação — ruído que esconderia justamente o que se quer provar.
MODELS = """
[[tier]]
name = "t0"
backend = "spy"
model = ""
max_turns = 3
cost_rank = 0

[router]
default_tier = "t0"
max_attempts = 3
min_n = 6
prior_floor = 0.50
"""

CATALOG = """
[improve]
sec_cost_usd = 0.0001
n_per_arm = 6

[[rule]]
id = "turns_up"
target_file = "config/models.toml"
key = "tier[0].max_turns"
from = 3
to = 9
fails_on = ["verify_failed"]
hypothesis = "sintética: run curta demais não converge"
"""

# A regra ao contrário, para o caso em que a candidata é PIOR que o baseline.
CATALOG_RUIM = CATALOG.replace("from = 3\nto = 9", "from = 9\nto = 3").replace(
    'id = "turns_up"', 'id = "turns_down"'
)

# Config de OUTRA árvore: se o ciclo ler daqui, o backend nem existe e a run
# explode — é o detector de "calibra uma árvore, mede outra".
MODELS_ISCA = MODELS.replace('backend = "spy"', 'backend = "isca_nao_registrada"').replace(
    "max_turns = 3", "max_turns = 99"
)


class SpyBackend:
    """Guarda cada `ExecRequest` e entrega o arquivo que o `verify` procura.

    `min_turns` é o que transforma a config em resultado: abaixo dele o espião
    não escreve nada e a unidade reprova. É o análogo determinístico de "o
    modelo não converge no orçamento de turnos que recebeu".
    """

    name = "spy"

    def __init__(self, seen: list, min_turns: int | None = None, boom: bool = False) -> None:
        self.seen = seen
        self.min_turns = min_turns
        self.boom = boom

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="espião de teste")

    def execute(self, req) -> ExecResult:
        self.seen.append(req)
        if self.boom:
            raise RuntimeError("espião morreu no meio")
        changed: tuple[str, ...] = ()
        if self.min_turns is None or req.max_turns >= self.min_turns:
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
    """Raiz isolada: `kinds.toml`/`genome.toml` reais, models e catálogo de teste."""
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    (tmp_path / "config" / "models.toml").write_text(MODELS, encoding="utf-8")
    (tmp_path / "config" / "catalog.toml").write_text(CATALOG, encoding="utf-8")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def spy():
    """Registra o backend `spy` e devolve a lista de requests que ele viu."""
    seen: list = []

    def install(min_turns: int | None = None, boom: bool = False) -> list:
        registry.register("spy", lambda: SpyBackend(seen, min_turns, boom))
        return seen

    try:
        yield install
    finally:
        registry.unregister("spy")


def db(sandbox: Path) -> Path:
    return sandbox / "data" / store.DB_NAME


def models(sandbox: Path) -> Path:
    return sandbox / "config" / "models.toml"


def seed_failures(sandbox: Path, n: int = 3) -> None:
    """Gradiente: sem falha no ledger, `pick_target` não tem o que atacar.

    `backend="mock"` de propósito — semente não é amostra do espião, e prior de
    (code, t0, spy) contaminado mudaria o tier no meio do experimento.
    """
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


def autopilot(sandbox: Path, **kw):
    """Sem `backend=`: quem escolhe é o router, que é o ponto do PR."""
    return run_autopilot(sandbox / "data", units=[UNIT], root=sandbox, **kw)


def runs_do_experimento(sandbox: Path) -> list[RunRow]:
    return [r for r in store.history(path=db(sandbox), limit=100) if r.run_id[:4] != "seed"]


# --- o canal ---------------------------------------------------------------------


def test_mutation_reaches_exec(sandbox, spy):
    """A prova: o mesmo backend, dois `max_turns` no `ExecRequest`, um por braço.

    12 runs alternadas A,B: a mutação `tier[0].max_turns 3->9` tem que aparecer
    exatamente nas do braço B. Com a `ArmSpec` montada uma vez na entrada do nó,
    a lista sai `[8]*12` (o DEFAULT_MAX_TURNS do CLI) — ou vazia, porque nem o
    backend do config era lido.
    """
    seen = spy()
    seed_failures(sandbox)

    autopilot(sandbox)

    assert [req.max_turns for req in seen] == [3, 9] * 6

    # A run também sai rotulada com o tier de quem a escolheu: sem isso o prior
    # do router (keyed em kind+tier+backend) nunca aprenderia com o A/B.
    linhas = runs_do_experimento(sandbox)
    assert {(r.backend, r.tier, r.kind) for r in linhas} == {("spy", "t0", "code")}


def test_ab_verdict_com_canal(sandbox, spy):
    """Espião que só passa com 5+ turnos: a mutação 3->9 é KEEP e a config FICA."""
    spy(min_turns=5)
    seed_failures(sandbox)

    report = autopilot(sandbox)

    r = report.results[0]
    assert (r["verdict"], r["arm_a"], r["arm_b"]) == ("KEEP", "0/6", "6/6")
    assert mutate.read_value(models(sandbox), KNOB) == 9
    assert store.mutations(path=db(sandbox))[0].verdict == "KEEP"


def test_ab_verdict_regra_ruim_e_discard(sandbox, spy):
    """A mesma régua ao contrário: 9->3 piora, DISCARD, e o toml volta igual."""
    models(sandbox).write_text(MODELS.replace("max_turns = 3", "max_turns = 9"), encoding="utf-8")
    (sandbox / "config" / "catalog.toml").write_text(CATALOG_RUIM, encoding="utf-8")
    antes = models(sandbox).read_bytes()
    spy(min_turns=5)
    seed_failures(sandbox)

    report = autopilot(sandbox)

    r = report.results[0]
    assert (r["verdict"], r["arm_a"], r["arm_b"]) == ("DISCARD", "6/6", "0/6")
    assert models(sandbox).read_bytes() == antes


def test_config_dir_pinado(sandbox, spy, monkeypatch):
    """`$HARNESS_CONFIG_DIR` de outra árvore não desvia o ciclo — e volta no fim.

    O loop muta `ROOT/config` e o router lê o env: apontando para árvores
    diferentes, o ciclo calibraria uma e mediria a outra.
    """
    isca = sandbox / "isca"
    shutil.copytree(sandbox / "config", isca)
    (isca / "models.toml").write_text(MODELS_ISCA, encoding="utf-8")
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(isca))
    seen = spy()
    seed_failures(sandbox)

    autopilot(sandbox)

    # 99 (ou zero run, com o backend da isca que nem existe) = leu a árvore errada
    assert [req.max_turns for req in seen] == [3, 9] * 6
    assert os.environ["HARNESS_CONFIG_DIR"] == str(isca)  # restaurado no fim


# --- ledger do experimento que não terminou --------------------------------------


def test_aborted_no_ledger(sandbox, spy):
    """Espião explodindo: a linha ABORTED é gravada NA PARADA, sem resume.

    Quem roda `harness improve` e nunca responde ao interrupt deixaria o toml
    calibrado por uma mutação que o ledger não conhece — e o replay do PR-10
    reconstruiria um histórico que não aconteceu.
    """
    spy(boom=True)
    seed_failures(sandbox)

    report = autopilot(sandbox)

    assert report.escalation["reason"] == escalate.ERROR
    linhas = store.mutations(path=db(sandbox))
    assert [(m.verdict, m.arm_a, m.arm_b, m.note) for m in linhas] == [
        ("ABORTED", "0/0", "0/0", "error")
    ]
    # a explosão foi na 1ª run do braço A, com a mutação desligada: o toml está
    # no baseline e a linha diz isso.
    assert linhas[0].reverted is True
    assert mutate.read_value(models(sandbox), KNOB) == 3


def test_cli_resume_abort(sandbox, spy, capsys):
    """`improve --resume <thread>` responde o interrupt pendente; default = abort."""
    from harness import cli

    spy(boom=True)
    seed_failures(sandbox)

    assert cli.main(["improve", "--unit", str(UNIT)]) == 0
    err = capsys.readouterr().err
    thread = re.search(r"thread=(\S+)", err).group(1)
    assert err.startswith("escalate error")

    assert cli.main(["improve", "--unit", str(UNIT), "--resume", thread]) == 0

    out = capsys.readouterr().out
    assert f"ciclo0 turns_up {KNOB} 3->9 ABORTED" in out
    assert "revertida" in out and "(error)" in out
    assert mutate.read_value(models(sandbox), KNOB) == 3
    # o resume não duplica a linha: o `mutation_id` é o mesmo experimento
    assert len(store.mutations(path=db(sandbox))) == 1


def test_cli_answer_sem_resume_e_erro_de_uso(sandbox):
    """`--answer` sozinho é resposta para interrupt nenhum: erro de uso, exit 2."""
    from harness import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["improve", "--unit", str(UNIT), "--answer", '{"action":"continue"}'])

    assert exc.value.code == 2
