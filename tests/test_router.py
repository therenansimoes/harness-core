"""O router é aritmética sobre metadado: nenhum teste aqui toca rede, LLM ou
backend. O que precisa ser verdade:

  - as 13 unidades da tabela caem no tier documentado (régua REAL — calibrar
    config/models.toml e mover um arquétipo tem que quebrar isto);
  - o prior é keyed em (kind, tier, backend): histórico ruim de (code, t0) NÃO
    contamina (content, t0) — é o bug do router velho que não pode voltar;
  - cada tentativa que falhou sobe exatamente um tier, clampando no topo;
  - config inválida derruba o load em vez de virar default silencioso.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.routing import CONFIG_DIR_ENV, router
from harness.types import RunRow, UnitSpec, Verdict

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def config_dir(monkeypatch):
    """select() classifica o kind lendo config/kinds.toml — o teste não pode
    depender do cwd de quem chamou o pytest."""
    monkeypatch.setenv(CONFIG_DIR_ENV, str(REPO / "config"))


@pytest.fixture
def cfg():
    return router.load_config(REPO / "config" / "models.toml")


def _unit(prompt: str, uid: str = "u", kind: str | None = None) -> UnitSpec:
    return UnitSpec(id=uid, path=Path("."), prompt=prompt, verify_cmd="true", kind=kind)


def _rows(n: int, kind: str, tier: str, backend: str, ok: bool) -> list[RunRow]:
    return [
        RunRow(
            run_id=f"r{i}", unit_id="u", project="p", backend=backend, model=None,
            tier=tier, kind=kind, ok=ok, exit_reason="done" if ok else "error",
            sec_total=1.0, sec_provision=0.1, cost_usd=0.0, intervention=False,
            created_at="2026-08-02T00:00:00+00:00",
        )
        for i in range(n)
    ]


def _verdict(passed: bool) -> Verdict:
    return Verdict(passed=passed, exit_code=0 if passed else 1, log_path=Path("v.log"), sec=0.1)


# ----------------------------------------------------------- as 13 unidades

# (id, prompt, kind esperado, tier esperado). Cobre os 5 kinds e os 3 tiers;
# a última é o fallback (nenhum sinal => code).
UNITS = [
    ("01", "ajustar o texto alternativo das imagens em src/pages/index.astro", "content", "t0"),
    ("02", "reescrever o texto da seção de contato em src/pages/contato.astro", "content", "t0"),
    ("03", "criar README.md com as instruções de uso", "content", "t0"),
    ("04", "ajustar o threshold de prior_floor em config/models.toml", "config", "t0"),
    ("05", "habilitar a flag de cache em app.yaml", "config", "t0"),
    ("06", "implementar a função de parse do CSV em harness/parse.py", "code", "t1"),
    ("07", "corrigir o bug de índice em src/list.ts", "code", "t1"),
    ("08", "escrever teste para o seletor em tests/test_router.py", "code", "t1"),
    ("09", "Dockerfile: subir o container do site com nginx", "infra", "t1"),
    ("10", "configurar o deploy no servidor com systemd", "infra", "t1"),
    ("11", "refatorar o schema de conteúdo do site em src/content/config.ts", "refactor", "t2"),
    ("12", "decidir a arquitetura de navegação e migrar as páginas", "refactor", "t2"),
    ("13", "atualizar os dados da planilha do orçamento", "code", "t1"),
]


def test_tem_13_unidades():
    assert len(UNITS) == 13


@pytest.mark.parametrize("uid,prompt,kind,tier", UNITS)
def test_unidades_caem_no_tier_documentado(cfg, uid, prompt, kind, tier):
    sel = router.select(_unit(prompt, uid), [], cfg=cfg)
    assert (sel.kind, sel.tier) == (kind, tier), sel.reasons


def test_selection_carrega_backend_modelo_e_turns(cfg):
    sel = router.select(_unit(UNITS[0][1]), [], cfg=cfg)
    t = router.tier_by_name(cfg, sel.tier)
    assert (sel.backend, sel.model, sel.max_turns) == (t.backend, t.model, t.max_turns)
    assert any(r.startswith("base:") for r in sel.reasons)


# -------------------------------------------------------------------- prior

# code e content no MESMO tier de partida: sem isso o teste de chave não prova
# nada (a diferença poderia vir do mapa kind->tier, não do prior).
PRIOR_TOML = """
[[tier]]
name = "t0"
backend = "deepagents"
model = "m0"
max_turns = 12
cost_rank = 0

[[tier]]
name = "t1"
backend = "deepagents"
model = "m1"
max_turns = 24
cost_rank = 1

[[tier]]
name = "t2"
backend = "claude_code"
model = ""
max_turns = 40
cost_rank = 2

[router]
default_tier = "t0"
max_attempts = 3
min_n = 6
prior_floor = 0.50

[router.kind]
code = "t0"
content = "t0"
"""


@pytest.fixture
def flat_cfg(tmp_path):
    p = tmp_path / "models.toml"
    p.write_text(PRIOR_TOML, encoding="utf-8")
    return router.load_config(p)


def test_prior_keyed(flat_cfg):
    """Aceite do PR: 6 falhas em (code, t0, deepagents) sobem a unit de code —
    e não encostam na unit de content, que roda no MESMO t0."""
    history = _rows(6, "code", "t0", "deepagents", ok=False)

    code = router.select(_unit("corrigir o bug em src/list.ts", kind="code"), history, cfg=flat_cfg)
    assert code.tier == "t1", code.reasons
    assert any(r.startswith("prior_bump:t0->t1") for r in code.reasons)

    content = router.select(_unit("reescrever o texto", kind="content"), history, cfg=flat_cfg)
    assert content.tier == "t0", content.reasons
    assert not [r for r in content.reasons if r.startswith("prior_")]


def test_prior_ignora_outro_backend(flat_cfg):
    history = _rows(6, "code", "t0", "outro", ok=False)
    sel = router.select(_unit("bug", kind="code"), history, cfg=flat_cfg)
    assert sel.tier == "t0", sel.reasons


def test_prior_ignora_amostra_pequena(flat_cfg):
    history = _rows(5, "code", "t0", "deepagents", ok=False)
    sel = router.select(_unit("bug", kind="code"), history, cfg=flat_cfg)
    assert sel.tier == "t0", sel.reasons
    assert not [r for r in sel.reasons if r.startswith("prior_")]


def test_prior_nao_sobe_com_historico_bom(flat_cfg):
    # 6/6 dá Wilson lower 0.61 — acima do floor 0.50, o tier fica.
    history = _rows(6, "code", "t0", "deepagents", ok=True)
    sel = router.select(_unit("bug", kind="code"), history, cfg=flat_cfg)
    assert sel.tier == "t0", sel.reasons


def test_prior_floor_e_configuravel(flat_cfg):
    history = _rows(6, "code", "t0", "deepagents", ok=True)  # lower = 0.61
    frouxo = {**flat_cfg, "router": {**flat_cfg["router"], "prior_floor": 0.10}}
    apertado = {**flat_cfg, "router": {**flat_cfg["router"], "prior_floor": 0.90}}
    assert router.select(_unit("bug", kind="code"), history, cfg=frouxo).tier == "t0"
    assert router.select(_unit("bug", kind="code"), history, cfg=apertado).tier == "t1"


def test_prior_sobe_em_cascata_ate_o_topo(flat_cfg):
    history = (
        _rows(6, "code", "t0", "deepagents", ok=False)
        + _rows(6, "code", "t1", "deepagents", ok=False)
    )
    sel = router.select(_unit("bug", kind="code"), history, cfg=flat_cfg)
    assert sel.tier == "t2", sel.reasons
    assert sel.backend == "claude_code"


def test_prior_no_topo_nao_estoura(flat_cfg):
    history = _rows(8, "code", "t2", "claude_code", ok=False)
    cfg = {**flat_cfg, "router": {**flat_cfg["router"], "kind": {"code": "t2"}}}
    sel = router.select(_unit("bug", kind="code"), history, cfg=cfg)
    assert sel.tier == "t2", sel.reasons
    assert any(r.startswith("prior_top:t2") for r in sel.reasons)


# --------------------------------------------------------------- escalation


def test_escalation_sobe_um_tier_por_attempt(cfg):
    unit = _unit(UNITS[0][1])  # content => t0
    assert router.select(unit, [], attempt=1, cfg=cfg).tier == "t1"
    assert router.select(unit, [], attempt=2, cfg=cfg).tier == "t2"


def test_escalation_clampa_no_topo(cfg):
    sel = router.select(_unit(UNITS[0][1]), [], attempt=5, cfg=cfg)
    assert sel.tier == "t2", sel.reasons
    assert any(r.startswith("attempt+5:") for r in sel.reasons)


def test_should_escalate(cfg):
    sel = router.select(_unit(UNITS[0][1]), [], cfg=cfg)  # t0
    assert router.should_escalate(sel, _verdict(False), 0, cfg) is True
    assert router.should_escalate(sel, _verdict(True), 0, cfg) is False
    # max_attempts = 3 inclui a primeira: depois da 3a (attempt=2) acabou.
    assert router.should_escalate(sel, _verdict(False), 2, cfg) is False


def test_should_escalate_falso_no_topo(cfg):
    topo = router.select(_unit(UNITS[10][1]), [], cfg=cfg)  # refactor => t2
    assert topo.tier == "t2"
    assert router.should_escalate(topo, _verdict(False), 0, cfg) is False


# --------------------------------------------------------------------- load


def test_load_valida_cost_rank_contiguo(tmp_path):
    bad = PRIOR_TOML.replace('name = "t2"\nbackend = "claude_code"\nmodel = ""\nmax_turns = 40\ncost_rank = 2',
                             'name = "t2"\nbackend = "claude_code"\nmodel = ""\nmax_turns = 40\ncost_rank = 3')
    p = tmp_path / "models.toml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(router.RouterError):
        router.load_config(p)


@pytest.mark.parametrize(
    "de,para",
    [
        ('default_tier = "t0"', 'default_tier = "t9"'),
        ('code = "t0"', 'code = "t9"'),
        ('code = "t0"', 'poesia = "t0"'),
        ("prior_floor = 0.50", "prior_floor = 1.50"),
        ("min_n = 6", "min_n = 0"),
        ("max_attempts = 3", "max_attempts = 0"),
        ('backend = "deepagents"\nmodel = "m0"', 'backend = ""\nmodel = "m0"'),
        ("max_turns = 12", "max_turns = 0"),
    ],
)
def test_load_rejeita_config_furada(tmp_path, de, para):
    p = tmp_path / "models.toml"
    p.write_text(PRIOR_TOML.replace(de, para, 1), encoding="utf-8")
    with pytest.raises(router.RouterError):
        router.load_config(p)


def test_load_arquivo_ausente(tmp_path):
    with pytest.raises(router.RouterError):
        router.load_config(tmp_path / "nao_existe.toml")


def test_config_real_carrega():
    cfg = router.load_config(REPO / "config" / "models.toml")
    assert [t.name for t in router.tiers(cfg)] == ["t0", "t1", "t2"]


def test_config_real_preserva_pricing():
    """models.toml tem dois donos: o router ([[tier]]/[router]) e o backend
    ([pricing]). Resolver um conflito jogando uma seção fora zera cost_usd em
    silêncio — o load do router aceita, ninguém quebra, a conta some."""
    cfg = router.load_config(REPO / "config" / "models.toml")
    assert cfg.get("pricing"), "config/models.toml perdeu a tabela [pricing] do backend"
