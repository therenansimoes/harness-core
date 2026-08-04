"""Governor: teto de gasto (`cost_cap_usd`) e prazo de soltura do banco
(`bench_cycles`) — funções puras + enforcement no gate do run."""

from pathlib import Path

import pytest

from harness.governor.governor import (
    CONTINUE,
    CUTOFF,
    Governor,
    bench_with_expiry,
    check_cost,
    load_gov,
)
from harness.graph.run_graph import run_unit
from harness.routing import CONFIG_DIR_ENV

# --- check_cost: cutoff no limiar, fail-open sem teto ---


def test_check_cost_corta_no_limiar_exato():
    gov = Governor(cost_cap_usd=1.0)
    assert check_cost(0.99, gov) == CONTINUE
    assert check_cost(1.0, gov) == CUTOFF  # limiar exato já corta
    assert check_cost(3.0, gov) == CUTOFF


def test_check_cost_sem_teto_nunca_corta():
    assert Governor().cost_cap_usd == 0.0  # default congelado = sem corte
    assert check_cost(9999.0, Governor()) == CONTINUE
    assert check_cost(9999.0, Governor(cost_cap_usd=0.0)) == CONTINUE


def test_check_cost_gasto_torto_nao_corta():
    gov = Governor(cost_cap_usd=1.0)
    assert check_cost(None, gov) == CONTINUE
    assert check_cost("caro", gov) == CONTINUE


def test_load_gov_cost_cap_zero_vale_e_negativo_cai_no_default(tmp_path):
    p = tmp_path / "governor.toml"
    p.write_text("[pressure]\ncost_cap_usd = 0.0\n", encoding="utf-8")
    assert load_gov(p).cost_cap_usd == 0.0  # 0 é resposta válida: sem corte
    p.write_text("[pressure]\ncost_cap_usd = -2.0\n", encoding="utf-8")
    assert load_gov(p).cost_cap_usd == Governor.cost_cap_usd
    p.write_text("[pressure]\ncost_cap_usd = 2.5\n", encoding="utf-8")
    assert load_gov(p).cost_cap_usd == 2.5


def test_load_gov_repo_config_tem_teto_de_gasto():
    gov = load_gov(Path(__file__).parent.parent / "config" / "governor.toml")
    assert gov.cost_cap_usd == 5.0


# --- bench_with_expiry: entra, cumpre bench_cycles, sai ---


def test_bench_expira_depois_de_bench_cycles():
    gov = Governor(bench_after=3, bench_cycles=2)
    stats = {"fria": {"proposals": 3, "keeps": 0}}

    banned, since = bench_with_expiry(stats, gov, 5, {})
    assert banned == {"fria"} and since == {"fria": 5}  # entrou no ciclo 5

    banned, since = bench_with_expiry(stats, gov, 6, since)
    assert banned == {"fria"} and since == {"fria": 5}  # 1 ciclo: ainda preso

    banned, since = bench_with_expiry(stats, gov, 7, since)
    assert banned == set() and since == {}  # 2 ciclos: solta e limpa a marca

    # Continua sem KEEP -> volta pro banco no ciclo seguinte, com marca nova.
    banned, since = bench_with_expiry(stats, gov, 8, since)
    assert banned == {"fria"} and since == {"fria": 8}


def test_bench_com_keep_nunca_entra_e_estado_fica_vazio():
    gov = Governor(bench_after=3, bench_cycles=2)
    stats = {"quente": {"proposals": 5, "keeps": 2}}
    assert bench_with_expiry(stats, gov, 3, {}) == (set(), {})


def test_bench_marca_torta_ou_do_futuro_reentra_no_ciclo_corrente():
    gov = Governor(bench_after=1, bench_cycles=2)
    stats = {"fria": {"proposals": 1, "keeps": 0}}
    banned, since = bench_with_expiry(stats, gov, 4, {"fria": "ontem"})
    assert banned == {"fria"} and since == {"fria": 4}
    banned, since = bench_with_expiry(stats, gov, 4, {"fria": 99})
    assert banned == {"fria"} and since == {"fria": 4}


def test_bench_por_celula_bane_no_kind_ruim_e_solta_no_kind_bom():
    gov = Governor(bench_after=3, bench_cycles=2)
    glob = {"pesquisa": {"proposals": 6, "keeps": 2}}
    code = {"pesquisa": {"proposals": 3, "keeps": 0}}
    content = {"pesquisa": {"proposals": 3, "keeps": 2}}

    banned, since = bench_with_expiry(glob, gov, 4, {}, kind="code", cell_stats=code)
    assert banned == {"pesquisa"} and since == {"code:pesquisa": 4}

    # Mesmo ciclo, outro kind: a célula de content colou KEEP -> a ação continua
    # viva lá, e a marca de code sobrevive intacta (banco de uma célula não é
    # perdoado porque o ciclo de hoje é de outra).
    banned, since = bench_with_expiry(glob, gov, 4, since, kind="content", cell_stats=content)
    assert banned == set() and since == {"code:pesquisa": 4}

    # O prazo de soltura corre por célula, como o banco.
    banned, since = bench_with_expiry(glob, gov, 6, since, kind="code", cell_stats=code)
    assert banned == set() and since == {}


def test_bench_por_celula_cai_no_global_quando_a_celula_esta_muda():
    gov = Governor(bench_after=3, bench_cycles=2)
    glob = {
        "fria": {"proposals": 5, "keeps": 0},
        "quente": {"proposals": 5, "keeps": 1},
    }
    # Kind sem amostra nenhuma: o agregado global julga — ausência de evidência
    # naquele kind não absolve quem nunca emplacou em lugar nenhum.
    banned, since = bench_with_expiry(glob, gov, 0, {}, kind="content", cell_stats={})
    assert banned == {"fria"} and since == {"content:fria": 0}

    # Sem kind é o caminho de sempre (marca com o nome nu), e a marca da célula
    # fica guardada para quando o kind dela voltar a rodar.
    banned, since = bench_with_expiry(glob, gov, 0, since)
    assert banned == {"fria"} and since == {"fria": 0, "content:fria": 0}


def test_bench_with_expiry_sem_estado_e_o_bench_de_sempre():
    gov = Governor(bench_after=3, bench_cycles=2)
    stats = {
        "fria": {"proposals": 3, "keeps": 0},
        "nova": {"proposals": 1, "keeps": 0},
    }
    banned, _ = bench_with_expiry(stats, gov, 0, None)
    assert banned == {"fria"}


# --- enforcement no grafo: gasto estourado termina em escalate ---


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _unit(tmp_path: Path, name: str, verify_cmd: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nprompt = "x"\nverify_cmd = "{verify_cmd}"\n',
        encoding="utf-8",
    )
    return unit


@pytest.fixture
def caro(monkeypatch):
    """Mock cobrando $2.50 por tentativa: o mock real reporta 0.0 (grátis) e
    nenhum teto o alcançaria."""
    from dataclasses import replace as _replace

    from harness.backends.mock import MockBackend

    original = MockBackend.execute
    monkeypatch.setattr(
        MockBackend,
        "execute",
        lambda self, req: _replace(original(self, req), cost_usd=2.5),
    )


def test_run_com_custo_estourado_escala_sem_retry(caro, data_dir, tmp_path, monkeypatch):
    """cost_cap_usd = 1.0 e a primeira tentativa custou 2.5: verify vermelho
    pediria retry (budget de sobra), mas o dinheiro acabou -> escalate."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "governor.toml").write_text("[pressure]\ncost_cap_usd = 1.0\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))

    unit = _unit(tmp_path, "gasto", "test -f nao_existe.txt")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-gasto", max_attempts=5)

    assert final["decision"].action == "escalate_human"
    assert "governor:custo_estourado" in final["decision"].reason
    assert final["attempt"] == 0  # não houve nova tentativa
    nodes = [e["node"] for e in final["events"]]
    assert "retry" not in nodes and "escalate" in nodes


def test_run_com_teto_folgado_mantem_retry(caro, data_dir, tmp_path, monkeypatch):
    """Teto acima do gasto acumulado: nada muda — o retry é o de sempre e a
    escalada vem do budget de tentativas, não do governor."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "governor.toml").write_text("[pressure]\ncost_cap_usd = 100.0\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))

    unit = _unit(tmp_path, "folga", "test -f nao_existe.txt")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-folga", max_attempts=2)

    assert final["decision"].action == "escalate_human"
    assert "governor" not in final["decision"].reason
    assert final["attempt"] == 1  # o retry rodou
