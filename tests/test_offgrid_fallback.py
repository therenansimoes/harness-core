"""Fallback off-grid: primário indisponível degrada para o tier local.

Zero rede e zero LLM — os backends são stubs registrados no `registry`, e o
`[fallback]` vem de um models.toml de tmpdir. Sondar o LM Studio de verdade aqui
faria o resultado do teste depender de o app estar aberto na máquina.
"""

import shutil
from pathlib import Path
from typing import ClassVar

import pytest

from harness import cli
from harness.backends import registry
from harness.backends.mock import MockBackend
from harness.backends.offgrid import LEDGER_NODE, resolve_backend
from harness.genome.genome import DEFAULT_PATH, check_patch, load
from harness.genome.tamper import IMMUTABLE_CHANGED, detect, fingerprint, immutable_files
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Preflight

REPO = Path(__file__).resolve().parents[1]
FIXTURE = str(Path(__file__).parent / "fixtures" / "echo")
GRAPH_FILE = "harness/graph/run_graph.py"

LOCAL_MODEL = "openai:qwen/qwen3.5-9b"
SEM_CLI = "claude CLI não encontrado — instale e autentique"

MODELS_TOML = """\
[[tier]]
name = "t0"
backend = "deepagents"
model = "{model}"
max_turns = 4
cost_rank = 0

[[tier]]
name = "t1"
backend = "claude_code"
model = "haiku"
max_turns = 4
cost_rank = 1

[router]
default_tier = "t0"
max_attempts = 2
min_n = 6
prior_floor = 0.50

[fallback]
offgrid = {offgrid}
tier = "t0"
"""


class StubBackend(MockBackend):
    """Mock com preflight comandado pelo teste — e `model` de verdade, para
    provar que o modelo do tier chega ao backend do fallback."""

    name: ClassVar[str] = "stub"
    model: str | None = None

    def __init__(self, pre: Preflight) -> None:
        self._pre = pre

    def preflight(self) -> Preflight:
        return self._pre


@pytest.fixture
def models(tmp_path):
    """Escreve o models.toml do teste. `models(offgrid=False)` desliga o gate."""

    def _write(*, offgrid: bool = True) -> Path:
        p = tmp_path / f"models-{offgrid}.toml"
        p.write_text(
            MODELS_TOML.format(model=LOCAL_MODEL, offgrid=str(offgrid).lower()),
            encoding="utf-8",
        )
        return p

    return _write


@pytest.fixture
def backends():
    """Registra stubs por nome e desfaz no fim — registro manual vence o embutido."""
    nomes: list[str] = []

    def _register(name: str, pre: Preflight) -> StubBackend:
        stub = StubBackend(pre)
        registry.register(name, lambda: stub)
        nomes.append(name)
        return stub

    yield _register
    for name in nomes:
        registry.unregister(name)


# ------------------------------------------------------- resolve_backend


def test_indisponivel_com_gate_ligado_degrada_para_o_tier_local(backends, models, capsys):
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    local = backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))

    res = resolve_backend("claude_code", "haiku", config_path=models())

    assert res.backend is local
    assert (res.name, res.model, res.tier) == ("deepagents", LOCAL_MODEL, "t0")
    assert res.degraded == {
        "intended_backend": "claude_code",
        "intended_model": "haiku",
        "reason": SEM_CLI,
    }
    # O modelo do tier tem que chegar ao backend, senão o fallback roda no que
    # sobrou do último run.
    assert local.model == LOCAL_MODEL
    # Degradar é barato, degradar em silêncio não: uma linha no stderr.
    assert "offgrid: claude_code indisponível" in capsys.readouterr().err


def test_gate_desligado_bloqueia_como_hoje(backends, models):
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))

    res = resolve_backend("claude_code", "haiku", config_path=models(offgrid=False))

    assert res.backend is None
    assert res.degraded is None
    assert res.preflight.reason == SEM_CLI


def test_preflight_ok_nao_degrada_nada(backends, models):
    primario = backends("claude_code", Preflight(ok=True, reason="2.1.220"))

    res = resolve_backend("claude_code", "haiku", config_path=models())

    assert res.backend is primario
    assert (res.name, res.model, res.tier, res.degraded) == ("claude_code", "haiku", None, None)


def test_config_invalida_da_unidade_propaga_blocked(backends, models):
    """Servidor de pé e modelo que ninguém serve é erro do PEDIDO: cair no
    fallback esconderia a config errada atrás de um run que "funcionou"."""
    motivo = "modelo 'qwen/inexistente' não está baixado/servido pelo LM Studio (tem: nenhum)"
    backends("claude_code", Preflight(ok=False, reason=motivo))
    backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))

    res = resolve_backend("claude_code", "haiku", config_path=models())

    assert res.backend is None
    assert res.degraded is None
    assert res.preflight.reason == motivo


def test_fallback_tambem_indisponivel_bloqueia_com_os_dois_motivos(backends, models):
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    backends("deepagents", Preflight(ok=False, reason="LM Studio não respondeu"))

    res = resolve_backend("claude_code", "haiku", config_path=models())

    assert res.backend is None
    assert res.degraded is None
    assert SEM_CLI in res.preflight.reason
    assert "LM Studio não respondeu" in res.preflight.reason


def test_cair_em_si_mesmo_nao_e_degradacao(backends, models):
    """Quem já é o tier off-grid não tem para onde cair — blocked, e o motivo é
    o original (não uma segunda cópia dele)."""
    backends("deepagents", Preflight(ok=False, reason="LM Studio não respondeu"))

    res = resolve_backend("deepagents", LOCAL_MODEL, config_path=models())

    assert res.backend is None
    assert res.preflight.reason == "LM Studio não respondeu"


def test_models_toml_ilegivel_desliga_o_fallback(backends, tmp_path, capsys):
    """Fail-open: config quebrada não vira exceção nova num caminho que já ia
    falhar — vira blocked e uma linha no stderr."""
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))

    res = resolve_backend("claude_code", "haiku", config_path=tmp_path / "nao-existe.toml")

    assert res.backend is None
    assert "offgrid: fallback desligado" in capsys.readouterr().err


def test_backend_desconhecido_continua_levantando(models):
    with pytest.raises(KeyError):
        resolve_backend("nao-existe", None, config_path=models())


# ------------------------------------------------------------- ledger/CLI


def test_run_degradado_grava_backend_real_e_o_no_do_fallback(
    backends, tmp_path, monkeypatch, capsys
):
    """Ponta a ponta pelo `harness run`: o ledger grava quem EXECUTOU (com o
    tier do fallback, senão o prior aprende de um par que não existiu) e a
    intenção sobrevive no nó `offgrid_fallback`."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "models.toml").write_text(
        MODELS_TOML.format(model=LOCAL_MODEL, offgrid="true"), encoding="utf-8"
    )
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))

    assert cli.main(["run", "--unit", FIXTURE, "--backend", "claude_code"]) == 0

    row = store.history()[0]
    assert (row.backend, row.model, row.tier) == ("deepagents", LOCAL_MODEL, "t0")
    assert store.get_node(row.run_id, LEDGER_NODE) == {
        "intended_backend": "claude_code",
        "intended_model": "",
        "reason": SEM_CLI,
    }
    assert "offgrid: claude_code indisponível" in capsys.readouterr().err


# ---------------------------------------------------------------- grafo


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """config/ e data/ só deste teste: models.toml com o `[fallback]` ligado e os
    dois tiers apontando para stubs. Devolve o data_dir que o grafo recebe."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "models.toml").write_text(
        MODELS_TOML.format(model=LOCAL_MODEL, offgrid="true"), encoding="utf-8"
    )
    # kinds.toml é o do repo: o kind é ortogonal ao fallback e tem teste próprio.
    shutil.copy(REPO / "config" / "kinds.toml", cfg / "kinds.toml")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    data = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(data))
    return data


def test_grafo_degradado_grava_o_backend_e_o_tier_de_quem_executou(backends, graph_env):
    """O `execute` do grafo degrada e a `Selection` degradada volta pro estado:
    é ela que o `record` lê, então a linha do ledger é do tier que pagou."""
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))

    final = run_unit(Path(FIXTURE), "claude_code", None, graph_env, thread_id="t-off")

    assert final["decision"].action == "accept"
    sel = final["selection"]
    assert (sel.backend, sel.model, sel.tier) == ("deepagents", LOCAL_MODEL, "t0")

    row = store.history()[0]
    assert (row.backend, row.model, row.tier) == ("deepagents", LOCAL_MODEL, "t0")
    # `exit_reason` é o do execute, não o do fallback: degradar não é desfecho.
    assert row.exit_reason == "done"
    assert store.get_node("t-off", LEDGER_NODE) == {
        "intended_backend": "claude_code",
        "intended_model": "",
        "reason": SEM_CLI,
        "backend": "deepagents",
        "model": LOCAL_MODEL,
        "tier": "t0",
        "attempt": 0,
    }
    exec_ev = [e for e in final["events"] if e["node"] == "execute"]
    assert [e.get("degraded") for e in exec_ev] == [True]


def test_grafo_sem_degradacao_nao_grava_no_nem_mexe_na_selecao(backends, graph_env):
    """Primário de pé: o caminho é o de sempre — nó do fallback ausente, seleção
    do route intacta e nenhum campo novo no evento."""
    backends("claude_code", Preflight(ok=True, reason="2.1.220"))

    final = run_unit(Path(FIXTURE), "claude_code", None, graph_env, thread_id="t-ok")

    sel = final["selection"]
    assert (sel.backend, sel.model, sel.tier) == ("claude_code", "", "manual")
    assert store.get_node("t-ok", LEDGER_NODE) is None
    row = store.history()[0]
    assert (row.backend, row.tier) == ("claude_code", "manual")
    assert not any(e.get("degraded") for e in final["events"])


def test_escalacao_para_tier_indisponivel_degrada_em_vez_de_bloquear(backends, graph_env, tmp_path):
    """Retry escala para o t1 e o t1 não existe nesta máquina: a tentativa cai no
    tier off-grid e o run termina pelo veredito da régua. Bloquear aqui trocaria
    um run com resposta por um run sem nenhuma."""
    backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    unit = tmp_path / "escala"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "escala"\nkind = "code"\nprompt = "x"\nverify_cmd = "test -f nao_existe.txt"\n',
        encoding="utf-8",
    )

    final = run_unit(unit, None, None, graph_env, thread_id="t-esc", max_attempts=2, route="auto")

    assert final["attempt"] == 1
    assert final["decision"].action == "escalate_human"
    sel = final["selection"]
    assert (sel.backend, sel.model, sel.tier) == ("deepagents", LOCAL_MODEL, "t0")

    # Attempt 0 rodou no t0, que está de pé: não há o que degradar lá.
    assert store.get_node("t-esc", LEDGER_NODE, attempt=0) is None
    deg = store.get_node("t-esc", LEDGER_NODE, attempt=1)
    assert (deg["intended_backend"], deg["backend"], deg["tier"]) == (
        "claude_code",
        "deepagents",
        "t0",
    )

    row = store.history()[0]
    assert (row.backend, row.tier, row.ok) == ("deepagents", "t0", False)
    assert row.exit_reason == "verify_failed"


def test_reuso_da_tentativa_preserva_backend_e_tier_degradados(backends, graph_env):
    """Segunda passagem no mesmo thread não reexecuta — e por isso precisa ler a
    degradação do ledger: sem isso o `record` de um resume gravaria o primário."""
    backends("claude_code", Preflight(ok=False, reason=SEM_CLI))
    backends("deepagents", Preflight(ok=True, reason="LM Studio ok"))

    run_unit(Path(FIXTURE), "claude_code", None, graph_env, thread_id="t-reuso")
    final = run_unit(Path(FIXTURE), "claude_code", None, graph_env, thread_id="t-reuso")

    # O trace da thread acumula as duas passagens: a segunda é a que reusa.
    exec_ev = [e for e in final["events"] if e["node"] == "execute"]
    assert [e.get("degraded") for e in exec_ev] == [True, None]
    assert [e.get("reused") for e in exec_ev] == [None, True]
    sel = final["selection"]
    assert (sel.backend, sel.model, sel.tier) == ("deepagents", LOCAL_MODEL, "t0")
    rows = store.history()
    assert len(rows) == 1
    assert (rows[0].backend, rows[0].tier) == ("deepagents", "t0")


# ----------------------------------------------------------------- genoma


def test_nada_do_fallback_toca_o_genoma():
    """O gate é config mutável e o hook mora nos backends: nenhum arquivo desta
    mudança pode estar na blocklist do genoma."""
    tocados = {
        "config/models.toml",
        "harness/backends/offgrid.py",
        "harness/cli.py",
        "tests/test_offgrid_fallback.py",
    }
    assert tocados.isdisjoint(immutable_files(load(DEFAULT_PATH), REPO))


def test_mutacao_nao_autorizada_do_grafo_continua_sendo_violacao(tmp_path):
    """O `run_graph` mudou UMA vez, com autorização do dono — o genoma não
    afrouxou por causa disso: patch que toca no grafo segue sendo violação
    declarada, e edição silenciosa segue sendo pega pelo fingerprint."""
    g = load(DEFAULT_PATH)
    assert check_patch(g, [GRAPH_FILE], root=REPO)

    # Cópia em tmpdir: a prova é sobre a régua, e mexer no arquivo de verdade
    # seria o próprio tamper que este teste diz ser proibido.
    sandbox = tmp_path / "repo"
    copia = sandbox / GRAPH_FILE
    copia.parent.mkdir(parents=True)
    copia.write_bytes((REPO / GRAPH_FILE).read_bytes())
    before = fingerprint(g, sandbox)

    dados = copia.read_bytes()
    copia.write_bytes(b"#" + dados[1:])  # um byte, e o fingerprint já muda

    assert IMMUTABLE_CHANGED in detect(sandbox, before, [GRAPH_FILE], genome=g)
