"""Fallback off-grid: primário indisponível degrada para o tier local.

Zero rede e zero LLM — os backends são stubs registrados no `registry`, e o
`[fallback]` vem de um models.toml de tmpdir. Sondar o LM Studio de verdade aqui
faria o resultado do teste depender de o app estar aberto na máquina.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from harness import cli
from harness.backends import registry
from harness.backends.mock import MockBackend
from harness.backends.offgrid import LEDGER_NODE, resolve_backend
from harness.genome.genome import DEFAULT_PATH, load
from harness.genome.tamper import immutable_files
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.types import Preflight

REPO = Path(__file__).resolve().parents[1]
FIXTURE = str(Path(__file__).parent / "fixtures" / "echo")

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
