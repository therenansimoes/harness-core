"""Reorg no runtime: o que o grafo APLICA e o que ele apenas grava.

Mesmo cenário de backends falsos do `test_run_graph` — nenhum modelo de verdade
é chamado aqui. O sinal entra pelo ledger (linhas de run semeadas na mão), que é
exatamente a fonte que o `_route` e o `_gate` leem em produção.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from harness.backends import deepagents_backend as da
from harness.backends import registry
from harness.governor import guards, reorg
from harness.graph import run_graph
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight, RunRow

REPO = Path(__file__).resolve().parent.parent
SPY_OUTPUT = "spy.txt"
LIXO_OUTPUT = "lixo.txt"
# Os ExecRequest que chegaram ao backend, na ordem. A frota decidida no route só
# é observável aqui: `Selection` some no fim do run, o request é o que o executor
# de verdade recebe.
SEEN: list[ExecRequest] = []

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


class SpyBackend:
    """Anota quem foi chamado com qual modelo: é o que torna o delta de tier do
    reorg observável — o nome do tier no evento não prova quem executou."""

    def __init__(self, name: str, calls: list[tuple]) -> None:
        self.name = name
        self.calls = calls

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
        SEEN.append(req)
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / SPY_OUTPUT).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(SPY_OUTPUT,),
            session_id=None,
            trace_path=req.trace_path,
        )


class CaroSpy(SpyBackend):
    """Primeira tentativa cara e sem entregar o arquivo do verify.

    É o único jeito honesto de o gasto do run passar do valor da tarefa antes de
    uma decisão de topologia: `_reorg_spent` só conhece custo de `execute` JÁ
    gravado, e na tentativa 0 o route roda antes de existir custo nenhum."""

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append((self.name, req.model, req.max_turns))
        SEEN.append(req)
        req.workspace.mkdir(parents=True, exist_ok=True)
        primeira = len(self.calls) == 1
        alvo = LIXO_OUTPUT if primeira else SPY_OUTPUT
        (req.workspace / alvo).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=1.0 if primeira else 0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(alvo,),
            session_id=None,
            trace_path=req.trace_path,
        )


class FalhaSpy(SpyBackend):
    """Nunca entrega o arquivo do verify: é o run que reprova em TODO tier —
    o cenário do guard de parada atada ao verify."""

    def execute(self, req: ExecRequest) -> ExecResult:
        self.calls.append((self.name, req.model, req.max_turns))
        SEEN.append(req)
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / LIXO_OUTPUT).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(LIXO_OUTPUT,),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def auto_config(tmp_path, monkeypatch):
    """config/ só deste teste: tiers falsos e NENHUM governor.toml — reorg cai
    nos defaults congelados, que é o caminho que se quer exercitar."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    shutil.copy(REPO / "config" / "kinds.toml", cfg / "kinds.toml")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


@pytest.fixture
def spies(auto_config):
    yield from _spies(SpyBackend)


@pytest.fixture
def spies_caros(auto_config):
    """Mesmos tiers, backend que cobra caro e falha o verify na primeira."""
    yield from _spies(CaroSpy)


@pytest.fixture
def spies_falhos(auto_config):
    """Mesmos tiers, backend que reprova o verify em toda tentativa."""
    yield from _spies(FalhaSpy)


def _spies(cls):
    SEEN.clear()
    calls: list[tuple] = []
    names = ("spy0", "spy1")
    for name in names:
        registry.register(name, (lambda n: lambda: cls(n, calls))(name))
    yield calls
    for name in names:
        registry.unregister(name)


def _unit(tmp_path: Path, name: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nkind = "code"\nprompt = "x"\nverify_cmd = "test -f {SPY_OUTPUT}"\n',
        encoding="utf-8",
    )
    return unit


def _seed_falhas(db: Path, n: int = 2, classe: str = "verify_failed") -> None:
    """`n` runs REPROVADAS da mesma classe, do mesmo kind — o sinal do R1."""
    for i in range(n):
        store.record_run(
            RunRow(
                run_id=f"seed{i}",
                unit_id=f"velha{i}",
                project=None,
                backend="spy0",
                model="m0",
                tier="t0",
                kind="code",
                ok=False,
                exit_reason=classe,
                sec_total=1.0,
                sec_provision=0.0,
                cost_usd=0.0,
                intervention=False,
                created_at=store.now_iso(),
            ),
            path=db,
        )


def _seed_area(db: Path, unit_id: str = "quente", n: int = 4) -> None:
    """`n` runs APROVADAS da mesma unidade — o sinal de área concentrada (R2).

    Aprovadas de propósito: falha ligaria também o R1 e o teste passaria a medir
    duas regras ao mesmo tempo."""
    for i in range(n):
        store.record_run(
            RunRow(
                run_id=f"area{i}",
                unit_id=unit_id,
                project=None,
                backend="spy0",
                model="m0",
                tier="t0",
                kind="code",
                ok=True,
                exit_reason="done",
                sec_total=1.0,
                sec_provision=0.0,
                cost_usd=0.0,
                intervention=False,
                created_at=store.now_iso(),
            ),
            path=db,
        )


def _reorg_rows(db: Path) -> list[dict]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT payload FROM node_events WHERE node = ? ORDER BY rowid", (reorg.NODE,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_regra_dispara_grava_node_reorg_com_sinal(data_dir, tmp_path, spies):
    """Decisão de topologia sem a evidência que a causou não é auditável: o
    payload carrega o sinal, não só o nome da regra."""
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    run_unit(_unit(tmp_path, "sinal"), None, None, data_dir, thread_id="t-sinal", route="auto")

    rows = _reorg_rows(db)
    assert [r["rule_id"] for r in rows] == [reorg.R_ESCALATE]
    assert rows[0]["state"] == "applied"
    assert rows[0]["effect"] == "applied"
    assert rows[0]["signal"] == {"failure_class": "verify_failed", "count": 2}
    assert rows[0]["run_id"] == "t-sinal"


def test_tier_sobe_um_nivel_quando_r1(data_dir, tmp_path, spies):
    """Duas falhas da mesma classe no ledger e a PRIMEIRA tentativa já roda um
    tier acima — quem paga muda, e é o backend chamado que prova."""
    _seed_falhas(data_dir / store.DB_NAME)
    final = run_unit(
        _unit(tmp_path, "sobe"), None, None, data_dir, thread_id="t-sobe", route="auto"
    )

    assert final["selection"].tier == "t1"
    assert spies == [("spy1", "m1", 5)]
    assert final["decision"].action == "accept"

    reasons = [r for e in final["events"] if e["node"] == "route" for r in e["reasons"]]
    assert "reorg:escalate_route:t0->t1" in reasons
    assert store.history()[0].backend == "spy1"


def test_sinal_limpa_grava_reversao(data_dir, tmp_path, spies):
    """O sinal sumiu do ledger: a decisão que ele justificava tem que cair.
    Topologia mudada por um sinal morto é dívida, não decisão."""
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    unit = _unit(tmp_path, "revert")
    run_unit(unit, None, None, data_dir, thread_id="t-rev", route="auto")
    assert [r["state"] for r in _reorg_rows(db)] == ["applied"]

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM runs WHERE ok = 0")
    run_unit(unit, None, None, data_dir, thread_id="t-rev", route="auto")

    rows = _reorg_rows(db)
    assert [r["state"] for r in rows] == ["applied", "reverted"]
    assert rows[1]["rule_id"] == reorg.R_ESCALATE
    assert rows[1]["cleared_signal"] == {"failure_class": "verify_failed", "count": 2}


def test_reorg_quebrado_nao_derruba_run(data_dir, tmp_path, spies, monkeypatch):
    """Reorg é opinião sobre o desenho do run. Opinião que estoura não pode
    levar o run junto: sem decisão, o grafo segue como antes de D5 existir."""
    _seed_falhas(data_dir / store.DB_NAME)

    def boom(*_a, **_k):
        raise RuntimeError("reorg pifou")

    monkeypatch.setattr(reorg, "decide", boom)
    final = run_unit(
        _unit(tmp_path, "boom"), None, None, data_dir, thread_id="t-boom", route="auto"
    )

    assert final["decision"].action == "accept"
    assert final["selection"].tier == "t0"  # sem reorg, o tier é o do router
    assert spies == [("spy0", "m0", 3)]
    assert _reorg_rows(data_dir / store.DB_NAME) == []
    # Fail-open também na frota: request idêntico ao de antes do reorg existir.
    assert SEEN[0].roles_allow is None
    assert SEEN[0].roles_required == ()


# --- frota: o que o executor recebe -------------------------------------------


def _valor_fixo(monkeypatch, valor: float) -> None:
    """`[reorg]` deste teste sem escrever toml: o único campo que interessa é o
    denominador do R3 (`collapse_fleet`), e ele é a config, não o ledger."""
    cfg = reorg.ReorgConfig(default_value_usd=valor)
    monkeypatch.setattr(reorg, "load_reorg", lambda *_a, **_k: cfg)


def test_area_concentrada_exige_reviewer(data_dir, tmp_path, spies):
    """Área concentrada não muda tier nenhum: o efeito é a FROTA. O request tem
    que sair com o papel exigido, senão a decisão continua sendo só uma linha
    bonita no ledger."""
    _seed_area(data_dir / store.DB_NAME)
    final = run_unit(
        _unit(tmp_path, "revisor"), None, None, data_dir, thread_id="t-rev-frota", route="auto"
    )

    assert final["decision"].action == "accept"
    assert final["selection"].tier == "t0"  # R2 não mexe em tier
    assert SEEN[0].roles_required == ("reviewer",)
    assert SEEN[0].roles_allow is None  # frota inteira, só com papel obrigatório

    reasons = [r for e in final["events"] if e["node"] == "route" for r in e["reasons"]]
    assert "reorg:insert_reviewer:roles+reviewer" in reasons
    rota = next(e for e in final["events"] if e["node"] == "route")
    assert rota["roles_required"] == ["reviewer"]
    assert "roles_allow" not in rota  # chave só aparece quando o reorg recortou


def test_gasto_acima_do_valor_colapsa_a_frota(data_dir, tmp_path, spies_caros, monkeypatch):
    """Tentativa 0 custou 100x o valor da tarefa: a segunda passagem roda sem
    frota. Delegar é mais token em cima de um orçamento já estourado."""
    _valor_fixo(monkeypatch, 0.01)
    final = run_unit(
        _unit(tmp_path, "caro"), None, None, data_dir, thread_id="t-caro", route="auto"
    )

    assert final["decision"].action == "accept"
    assert len(SEEN) == 2
    assert SEEN[0].roles_allow is None  # sem gasto ainda, nada a colapsar
    assert SEEN[1].roles_allow == ()
    assert SEEN[1].roles_required == ()

    rota = [e for e in final["events"] if e["node"] == "route"][-1]
    assert rota["roles_allow"] == []
    assert "reorg:collapse_fleet:roles=0" in rota["reasons"]


def test_colapso_vence_o_revisor(data_dir, tmp_path, spies_caros, monkeypatch):
    """As duas regras juntas: sem dinheiro não se compra revisor. A precedência
    é explícita porque o contrário (papel exigido numa frota vazia) mandaria o
    modelo chamar um `subagent_type` que não existe."""
    _valor_fixo(monkeypatch, 0.01)
    _seed_area(data_dir / store.DB_NAME)
    run_unit(_unit(tmp_path, "ambos"), None, None, data_dir, thread_id="t-ambos", route="auto")

    assert SEEN[0].roles_required == ("reviewer",)  # tentativa 0: só o R2
    assert SEEN[1].roles_allow == ()
    assert SEEN[1].roles_required == ()


def test_frota_intacta_quando_nenhuma_regra_dispara(data_dir, tmp_path, spies):
    """Run comum: ledger vazio, nenhuma decisão, request byte a byte o de antes
    — e o evento `route` sem as chaves novas."""
    final = run_unit(
        _unit(tmp_path, "normal"), None, None, data_dir, thread_id="t-normal", route="auto"
    )

    assert SEEN[0].roles_allow is None
    assert SEEN[0].roles_required == ()
    rota = next(e for e in final["events"] if e["node"] == "route")
    assert "roles_allow" not in rota
    assert "roles_required" not in rota


# --- frota: o que o backend faz com ela ---------------------------------------


PAPEIS = [
    {"name": "planner", "description": "planeja"},
    {"name": "reviewer", "description": "revisa"},
]


def _req(tmp_path: Path, **kw) -> ExecRequest:
    return ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx", **kw)


def test_fleet_sem_reorg_devolve_a_frota_do_toml(tmp_path):
    assert da._fleet(PAPEIS, _req(tmp_path)) == (PAPEIS, "")


def test_fleet_colapsada_e_vazia_de_verdade(tmp_path):
    """`roles_allow=()` é frota vazia, não "sem restrição": a lista sai vazia
    (nenhum subagent chega ao create_deep_agent) E o prompt ganha a ordem."""
    roles, ordem = da._fleet(PAPEIS, _req(tmp_path, roles_allow=()))
    assert roles == []
    assert "não chame a tool `task`" in ordem


def test_fleet_filtra_pelos_nomes_permitidos(tmp_path):
    roles, ordem = da._fleet(PAPEIS, _req(tmp_path, roles_allow=("reviewer",)))
    assert [r["name"] for r in roles] == ["reviewer"]
    assert ordem == ""


def test_fleet_exige_papel_que_existe(tmp_path):
    roles, ordem = da._fleet(PAPEIS, _req(tmp_path, roles_required=("reviewer",)))
    assert roles == PAPEIS
    assert "task(subagent_type='reviewer')" in ordem


def test_fleet_nao_inventa_papel(tmp_path):
    """Papel exigido que não existe no agents.toml não vira ordem: o modelo
    chamaria um `subagent_type` inexistente e queimaria o turno no erro."""
    assert da._fleet(PAPEIS, _req(tmp_path, roles_required=("auditor",))) == (PAPEIS, "")


def test_fleet_torto_devolve_a_frota_intacta():
    """Fail-open: request que estoura no acesso ao campo não derruba execução."""

    class Torto:
        @property
        def roles_allow(self):
            raise RuntimeError("boom")

    assert da._fleet(PAPEIS, Torto()) == (PAPEIS, "")


def _kwargs_do_agente(monkeypatch, req: ExecRequest) -> dict:
    """O que chega ao `create_deep_agent`. Nenhum modelo é chamado."""
    pytest.importorskip("deepagents")
    import deepagents

    capturado: dict = {}

    def spy(*a, **kw):
        capturado.update(kw)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", spy)
    da._build_agent(req)
    return capturado


def test_colapso_tira_os_subagents_do_agente(tmp_path, monkeypatch):
    """O colapso tem que ser real, não uma frase no prompt: sem `subagents` a
    tool `task` volta ao `general-purpose` default."""
    kw = _kwargs_do_agente(monkeypatch, _req(tmp_path, roles_allow=()))
    assert "subagents" not in kw
    assert "não chame a tool `task`" in kw["system_prompt"]


def test_papel_exigido_vira_ordem_no_system_prompt(tmp_path, monkeypatch):
    """O papel exigido é nome; quem instancia é o executor, com o agents.toml
    do repo. A ordem vem DEPOIS do manual dos papéis."""
    kw = _kwargs_do_agente(monkeypatch, _req(tmp_path, roles_required=("reviewer",)))
    prompt = kw["system_prompt"]
    assert "reviewer" in [s["name"] for s in kw["subagents"]]
    assert "antes de terminar, chame task(subagent_type='reviewer')" in prompt
    assert prompt.index("Você pode delegar") < prompt.index("Este run tem papel OBRIGATÓRIO")


# --- guards: o freio do reorg --------------------------------------------------


def _guards_fixos(monkeypatch, **over) -> None:
    """`[guards]` deste teste sem escrever toml, no molde do `_valor_fixo`."""
    cfg = guards.GuardsConfig(**over)
    monkeypatch.setattr(guards, "load_guards", lambda *_a, **_k: cfg)


def _route_reasons(final) -> list[str]:
    return [r for e in final["events"] if e["node"] == "route" for r in e["reasons"]]


def test_budget_estourado_para_escalada(data_dir, tmp_path, spies_caros, monkeypatch):
    """Tentativa 0 custou mais que o teto: a segunda passagem NÃO sobe para o
    spy1 — a escalada por tentativa do router é desfeita — mas o run segue e
    fecha aceito. Guard para de escalar, nunca mata."""
    _guards_fixos(monkeypatch, max_cost_usd=0.5)
    db = data_dir / store.DB_NAME
    final = run_unit(
        _unit(tmp_path, "freio"), None, None, data_dir, thread_id="t-freio", route="auto"
    )

    assert final["decision"].action == "accept"
    assert spies_caros == [("spy0", "m0", 3), ("spy0", "m0", 3)]
    assert any(r.startswith("reorg:guard:budget:cost") for r in _route_reasons(final))

    rows = [r for r in _reorg_rows(db) if r["rule_id"] == guards.G_BUDGET]
    assert len(rows) == 1
    assert (rows[0]["state"], rows[0]["effect"]) == ("applied", "applied")
    assert rows[0]["signal"]["kind"] == "cost"
    assert rows[0]["run_id"] == "t-freio"


def test_budget_suprime_r1_e_revisor(data_dir, tmp_path, spies, monkeypatch):
    """R1 (tier) e R2 (revisor) com sinal vivo no ledger, mas o relógio do run
    estourou: nenhum dos dois chega ao request — sem subir tier, sem papel
    exigido. O guard corta a ESCALADA, não a tentativa."""
    _guards_fixos(monkeypatch, max_wall_s=1.0)
    monkeypatch.setattr(run_graph, "_guard_elapsed", lambda *_a, **_k: 1e9)
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    # n=3: com as 2 falhas dá 5 runs, abaixo do `min_n=6` do router — senão o
    # prior bump do PRÓPRIO router subiria o tier e o teste mediria outra coisa.
    _seed_area(db, n=3)
    final = run_unit(
        _unit(tmp_path, "parede"), None, None, data_dir, thread_id="t-parede", route="auto"
    )

    assert final["decision"].action == "accept"
    assert spies == [("spy0", "m0", 3)]  # sem subida de tier apesar do R1
    assert SEEN[0].roles_required == ()  # sem revisor apesar do R2
    assert any(r.startswith("reorg:guard:budget:wall") for r in _route_reasons(final))

    rows = _reorg_rows(db)
    budget_rows = [r for r in rows if r["rule_id"] == guards.G_BUDGET]
    assert len(budget_rows) == 1
    assert budget_rows[0]["signal"]["kind"] == "wall"
    # O sinal do R2 estava VIVO (o gate anotou a decisão) — a frota limpa do
    # request acima é obra do guard, não de regra dormindo.
    assert reorg.R_REVIEWER in [r["rule_id"] for r in rows]


def test_verify_stop_no_topo_fecha_com_falha(data_dir, tmp_path, spies_falhos, monkeypatch):
    """Dois verifies vermelhos seguidos no tier de cima: não há para onde
    escalar, o gate para de tentar e o run fecha com relatório honesto de
    falha — ok=False, verify_failed, ids de guard no evento do gate."""
    _guards_fixos(monkeypatch, verify_fail_stop=2)
    db = data_dir / store.DB_NAME
    final = run_unit(
        _unit(tmp_path, "teto"),
        None,
        None,
        data_dir,
        thread_id="t-teto",
        route="auto",
        max_attempts=5,
    )

    assert len(spies_falhos) == 3  # t0, t1, t1 — e o guard corta a quarta
    assert final["decision"].action == "escalate_human"
    gate = [e for e in final["events"] if e["node"] == "gate"][-1]
    assert "guard:verify_stop:2x_top_tier" in gate["reason"]
    assert "guard:verify_stop" in gate["guards"]

    rows = [r for r in _reorg_rows(db) if r["rule_id"] == guards.G_VERIFY]
    assert len(rows) == 1
    row = store.history()[0]
    assert row.ok is False
    assert row.exit_reason == "verify_failed"


def test_flipflop_congela_topologia(data_dir, tmp_path, spies):
    """A mesma regra aplicada -> revertida -> aplicada neste run: oscilação.
    O guard congela a topologia — nem o R1 com sinal vivo sobe tier — e a
    linha de freeze fecha o nó do reorg pelo resto do run."""
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    for i, estado in enumerate(["applied", "reverted", "applied"]):
        store.record_node(
            "t-flip",
            reorg.NODE,
            {"rule_id": reorg.R_ESCALATE, "state": estado, "run_id": "t-flip"},
            db,
            attempt=i,
        )
    final = run_unit(
        _unit(tmp_path, "flip"), None, None, data_dir, thread_id="t-flip", route="auto"
    )

    assert final["decision"].action == "accept"
    assert spies == [("spy0", "m0", 3)]  # sem subida de tier apesar do R1
    assert "reorg:guard:freeze:escalate_route" in _route_reasons(final)

    rows = _reorg_rows(db)
    assert len(rows) == 4  # 3 semeadas + o freeze; o gate não gravou mais nada
    assert rows[-1]["rule_id"] == guards.G_FREEZE
    assert rows[-1]["state"] == "applied"
    assert rows[-1]["signal"]["rule_id"] == reorg.R_ESCALATE


def test_frozen_run_ignora_reorg_e_avisa(data_dir, tmp_path, spies):
    """Run que já tem linha de freeze: o reorg nem decide nem grava — só o
    aviso na rota de que a topologia está congelada."""
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    store.record_node(
        "t-frozen",
        reorg.NODE,
        {"rule_id": guards.G_FREEZE, "state": "applied", "run_id": "t-frozen"},
        db,
        attempt=0,
    )
    final = run_unit(
        _unit(tmp_path, "frozen"), None, None, data_dir, thread_id="t-frozen", route="auto"
    )

    assert final["decision"].action == "accept"
    assert spies == [("spy0", "m0", 3)]
    assert "reorg:guard:freeze:active" in _route_reasons(final)
    assert len(_reorg_rows(db)) == 1  # só a linha semeada: nada novo no nó


def test_guards_desligado_mantem_comportamento(data_dir, tmp_path, spies, monkeypatch):
    """`enabled=false` devolve o runtime pré-guards byte a byte: o R1 sobe o
    tier como sempre e nenhuma linha de guard aparece no ledger."""
    _guards_fixos(monkeypatch, enabled=False)
    db = data_dir / store.DB_NAME
    _seed_falhas(db)
    final = run_unit(
        _unit(tmp_path, "liga"), None, None, data_dir, thread_id="t-liga", route="auto"
    )

    assert final["selection"].tier == "t1"
    assert spies == [("spy1", "m1", 5)]
    assert "reorg:escalate_route:t0->t1" in _route_reasons(final)
    assert [r for r in _reorg_rows(db) if str(r.get("rule_id", "")).startswith("guard:")] == []
