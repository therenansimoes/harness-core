import shutil
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from harness.backends import registry
from harness.graph import checkpoint
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

REPO = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "echo"
FLAKY_OUTPUT = "ok.txt"
SPY_OUTPUT = "spy.txt"

# models.toml de teste: dois tiers, cada um com backend E modelo diferentes, os
# dois falsos. Regra de hardware — nenhum teste aqui chama modelo de verdade.
# É o que deixa o assert da escalada ser sobre quem executou, não sobre o nome
# do tier.
MODELS_TOML = """\
[[tier]]
name = "t0"
backend = "spy0"
model = "m0"
max_turns = 3
cost_rank = 0

[[tier]]
name = "t1"
backend = "spy1"
model = "m1"
max_turns = 5
cost_rank = 1

[router]
default_tier = "t0"
max_attempts = 3
min_n = 6
prior_floor = 0.50

[router.kind]
code = "t0"
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _count(db: Path, sql: str, *params) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchone()[0]


class FlakyBackend:
    """Só produz o arquivo que o verify procura a partir da chamada `succeed_on`.

    É o backend estocástico do mundo real em miniatura: a 1ª tentativa falha a
    régua, a 2ª passa. Com marcador por-run (sem attempt) o retry devolveria o
    ExecResult cacheado e a unidade nunca passaria.
    """

    name: ClassVar[str] = "flaky"

    def __init__(self, calls: list[int], succeed_on: int) -> None:
        self.calls = calls
        self.succeed_on = succeed_on

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

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append(len(self.calls))
        if len(self.calls) >= self.succeed_on:
            req.workspace.mkdir(parents=True, exist_ok=True)
            (req.workspace / FLAKY_OUTPUT).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(FLAKY_OUTPUT,),
            session_id=None,
            trace_path=req.trace_path,
        )


class SpyBackend:
    """Anota quem foi chamado com qual modelo — é o que torna a escolha do
    router observável sem backend de verdade. Quem decide se o run passa ou
    entra em retry é o `verify_cmd` da unidade, não o spy."""

    def __init__(self, name: str, calls: list[tuple], output: str) -> None:
        self.name = name
        self.calls = calls
        self.output = output

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=True,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append((self.name, req.model, req.max_turns))
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / self.output).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(self.output,),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def auto_config(tmp_path, monkeypatch):
    """config/ só deste teste: `HARNESS_CONFIG_DIR` faz o router ler os tiers
    falsos em vez do models.toml do repo (que aponta pra modelo de verdade)."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    # kinds.toml é o do repo: o kind é ortogonal ao tier e não é o que se testa
    # aqui — o classificador tem os testes dele em test_kinds.py.
    shutil.copy(REPO / "config" / "kinds.toml", cfg / "kinds.toml")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


@pytest.fixture
def spies(auto_config):
    """Registra os backends dos dois tiers do models.toml de teste; devolve a
    lista `(backend, model, max_turns)` de cada execução, na ordem."""
    calls: list[tuple] = []
    names = ("spy0", "spy1")

    def make(name: str):
        return lambda: SpyBackend(name, calls, SPY_OUTPUT)

    for name in names:
        registry.register(name, make(name))
    yield calls
    for name in names:
        registry.unregister(name)


@pytest.fixture
def flaky(tmp_path):
    """Registra o backend flaky; devolve a lista de chamadas para o assert."""
    calls: list[int] = []
    registry.register("flaky", lambda: FlakyBackend(calls, succeed_on=2))
    yield calls
    registry.unregister("flaky")


def _unit(tmp_path: Path, name: str, verify_cmd: str, kind: str | None = None) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    kind_line = f'kind = "{kind}"\n' if kind else ""
    (unit / "unit.toml").write_text(
        f'id = "{name}"\n{kind_line}prompt = "x"\nverify_cmd = "{verify_cmd}"\n',
        encoding="utf-8",
    )
    return unit


def test_run_unit_accepts_and_records(data_dir):
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-accept")

    assert final["decision"].action == "accept"
    assert final["verdict"].passed is True
    assert final["exec"].ok is True
    assert final["run_id"] == "t-accept"
    assert Path(final["workspace"]).is_dir()

    rows = store.history()
    assert len(rows) == 1
    assert rows[0].run_id == "t-accept"
    assert rows[0].backend == "mock"
    assert rows[0].kind == "code"
    assert rows[0].ok is True
    assert rows[0].exit_reason == "done"

    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 1
    assert store.get_node("t-accept", "execute")["exit_reason"] == "done"


def test_events_are_the_trace(data_dir):
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-trace")
    nodes = [e["node"] for e in final["events"]]
    assert nodes == [
        "plan",
        "route",
        "provision",
        "execute",
        "verify",
        "measure",
        "gate",
        "accept",
        "record",
    ]


def test_failed_verify_escalates_after_max_attempts(data_dir, tmp_path):
    unit = _unit(tmp_path, "bad", "test -f nao_existe.txt")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-fail", max_attempts=2)

    assert final["decision"].action == "escalate_human"
    assert final["attempt"] == 1
    rows = store.history()
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].exit_reason == "verify_failed"

    # Cada tentativa é uma execução de verdade: 2 attempts = 2 pares execute/verify.
    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 2
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'verify'") == 2
    # Só o workspace é reusado entre tentativas; execute/verify, nunca.
    assert not any(e.get("reused") for e in final["events"] if e["node"] != "provision")


def test_retry_reexecutes_and_can_change_the_outcome(data_dir, tmp_path, flaky):
    # A 1ª execução não cria `ok.txt` → verify falha → gate manda retry.
    unit = _unit(tmp_path, "flaky", f"test -f {FLAKY_OUTPUT}")
    final = run_unit(unit, "flaky", None, data_dir, thread_id="t-retry", max_attempts=3)

    assert len(flaky) == 2, "o backend precisa rodar de novo na 2ª tentativa"
    assert final["attempt"] == 1
    assert final["decision"].action == "accept"
    assert final["verdict"].passed is True

    # `reflect` entra na perna do retry pela topologia default (é o checker que
    # monta o hint da tentativa seguinte) — o resto do caminho é o de sempre.
    nodes = [e["node"] for e in final["events"]]
    assert nodes == [
        "plan",
        "route",
        "provision",
        "execute",
        "verify",
        "measure",
        "gate",
        "retry",
        "reflect",
        "route",
        "provision",
        "execute",
        "verify",
        "measure",
        "gate",
        "accept",
        "record",
    ]
    # Nenhum payload cacheado atravessou a fronteira da tentativa.
    assert not any(e.get("reused") for e in final["events"] if e["node"] != "provision")
    assert [e["attempt"] for e in final["events"] if e["node"] == "execute"] == [0, 1]

    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 2
    assert _count(db, "SELECT COUNT(*) FROM runs") == 1
    assert store.history()[0].ok is True


def test_route_auto_usa_router(data_dir, tmp_path, spies):
    """Sem backend fixado, quem escolhe é o router: kind=code cai no tier
    inicial do [router.kind], e é o backend/model DESSE tier que executa."""
    unit = _unit(tmp_path, "auto", f"test -f {SPY_OUTPUT}", kind="code")
    final = run_unit(unit, None, None, data_dir, thread_id="t-auto", route="auto")

    sel = final["selection"]
    assert (sel.tier, sel.kind) == ("t0", "code")
    assert (sel.backend, sel.model, sel.max_turns) == ("spy0", "m0", 3)
    assert spies == [("spy0", "m0", 3)]
    assert final["decision"].action == "accept"

    # A escolha vai pro trace com o porquê, não só com o resultado.
    route_ev = next(e for e in final["events"] if e["node"] == "route")
    assert route_ev["reasons"] == ["explicit:code", "base:code->t0"]

    row = store.history()[0]
    assert (row.tier, row.kind, row.backend, row.model) == ("t0", "code", "spy0", "m0")


def test_route_manual_intocado(data_dir, tmp_path, monkeypatch):
    """Chamada com backend fixado não consulta o router. O config aponta para um
    diretório vazio de propósito: se o nó tentasse carregar models.toml, o load
    estouraria em vez de passar."""
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "sem-config"))
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-manual")

    sel = final["selection"]
    assert (sel.backend, sel.model, sel.tier) == ("mock", "", "manual")
    assert sel.reasons == ("manual:pedido_do_chamador",)
    assert final["decision"].action == "accept"

    row = store.history()[0]
    assert (row.tier, row.backend, row.kind) == ("manual", "mock", "code")


def test_retry_escalada(data_dir, tmp_path, spies):
    """Verify falha no attempt 0 => o attempt 1 roda um tier acima. Sem isto o
    braço retry seria só repetição: mesma conta, mesmo resultado esperado."""
    unit = _unit(tmp_path, "escala", "test -f nao_existe.txt", kind="code")
    final = run_unit(unit, None, None, data_dir, thread_id="t-escala", max_attempts=2, route="auto")

    assert final["attempt"] == 1
    assert final["decision"].action == "escalate_human"
    # Backend E modelo mudaram entre as tentativas — é o tier de cima pagando.
    assert spies == [("spy0", "m0", 3), ("spy1", "m1", 5)]

    tiers = [e["tier"] for e in final["events"] if e["node"] == "route"]
    assert tiers == ["t0", "t1"]
    assert "attempt+1:t0->t1" in [
        r for e in final["events"] if e["node"] == "route" for r in e["reasons"]
    ]

    db = data_dir / store.DB_NAME
    assert store.get_node("t-escala", "execute", db, attempt=1) is not None
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 2

    # A linha do ledger é do ÚLTIMO attempt: quem pagou a conta foi o t1.
    row = store.history()[0]
    assert (row.tier, row.backend, row.model, row.ok) == ("t1", "spy1", "m1", False)


def test_route_auto_recusa_backend_fixado(data_dir):
    with pytest.raises(ValueError, match="router"):
        run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-conflito", route="auto")


def test_reinvoke_of_finished_thread_keeps_one_ledger_row(data_dir):
    # Nó já marcado em node_events não repete escrita externa, mesmo em re-run.
    run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-again")
    run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-again")

    db = data_dir / store.DB_NAME
    assert _count(db, "SELECT COUNT(*) FROM runs") == 1
    assert _count(db, "SELECT COUNT(*) FROM node_events WHERE node = 'execute'") == 1


def test_provision_reuses_workspace(data_dir):
    final = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-ws")
    ws = Path(final["workspace"])
    assert ws == data_dir / "ws" / "t-ws"

    marker = ws / "sobrevivi.txt"
    marker.write_text("x", encoding="utf-8")
    again = run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-ws")
    assert Path(again["workspace"]) == ws
    assert marker.is_file()


def test_checkpointer_writes_its_own_db(data_dir):
    run_unit(FIXTURE, "mock", None, data_dir, thread_id="t-cp")
    assert checkpoint.checkpoint_path(data_dir).is_file()


def test_bootstrap_locks_serde_and_kills_tracing(monkeypatch):
    for var in ("LANGGRAPH_STRICT_MSGPACK", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
        monkeypatch.delenv(var, raising=False)
    checkpoint.bootstrap_env()
    import os

    assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["LANGSMITH_TRACING"] == "false"
