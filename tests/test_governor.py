"""Governor: prazo, pressão e foco — funções puras + enforcement no grafo."""

from pathlib import Path

import pytest

from harness.governor.governor import (
    CONTINUE,
    CUTOFF,
    Governor,
    bench,
    check_deadline,
    explore_budget,
    load_gov,
    taper_turns,
)
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV

# --- load_gov: fail-open ---


def test_load_gov_toml_ausente_devolve_defaults(tmp_path):
    gov = load_gov(tmp_path / "nao_existe.toml")
    assert gov == Governor()
    # sem config, pressão zero: taper 1.0 mantém o comportamento atual do grafo
    assert gov.turn_taper == 1.0


def test_load_gov_toml_torto_cai_no_default_campo_a_campo(tmp_path):
    p = tmp_path / "governor.toml"
    p.write_text(
        '[deadline]\nrun_s = "muito"\ncycle_s = 60.0\n'
        "[pressure]\nturn_taper = 3.0\n"
        "[focus]\nbench_after = 0\n",
        encoding="utf-8",
    )
    gov = load_gov(p)
    assert gov.run_s == Governor.run_s  # torto -> default
    assert gov.cycle_s == 60.0  # válido -> vale
    assert gov.turn_taper == 1.0  # fora de [0,1] -> default
    assert gov.bench_after == Governor.bench_after  # < 1 -> default


def test_load_gov_ilegivel_devolve_defaults(tmp_path):
    p = tmp_path / "governor.toml"
    p.write_text("isto nao é toml [[[", encoding="utf-8")
    assert load_gov(p) == Governor()


def test_load_gov_repo_config_valido():
    gov = load_gov(Path(__file__).parent.parent / "config" / "governor.toml")
    assert gov.run_s == 900.0
    assert gov.turn_taper == 0.75


# --- check_deadline: cutoff exato no limiar ---


def test_check_deadline_antes_continua_e_no_limiar_corta():
    gov = Governor(run_s=10.0)
    assert check_deadline(100.0, 109.999, gov) == CONTINUE
    assert check_deadline(100.0, 110.0, gov) == CUTOFF  # limiar exato já corta
    assert check_deadline(100.0, 200.0, gov) == CUTOFF


# --- taper_turns: decresce e satura em 1 ---


def test_taper_turns_decresce_e_satura_em_um():
    gov = Governor(turn_taper=0.5)
    assert taper_turns(8, 0, gov) == 8
    assert taper_turns(8, 1, gov) == 4
    assert taper_turns(8, 2, gov) == 2
    assert taper_turns(8, 3, gov) == 1
    assert taper_turns(8, 50, gov) == 1  # nunca abaixo de 1
    assert taper_turns(1, 0, gov) == 1


def test_taper_turns_sem_config_e_noop():
    gov = Governor()  # taper 1.0
    assert taper_turns(8, 5, gov) == 8


# --- explore_budget: interpola e clampa ---


def test_explore_budget_interpola_linear():
    gov = Governor(explore_frac_start=0.5, explore_frac_end=0.0)
    assert explore_budget(0.0, gov) == 0.5
    assert explore_budget(0.5, gov) == pytest.approx(0.25)
    assert explore_budget(1.0, gov) == 0.0


def test_explore_budget_clampa_entrada_e_saida():
    gov = Governor(explore_frac_start=0.5, explore_frac_end=0.0)
    assert explore_budget(-3.0, gov) == 0.5  # antes do início = início
    assert explore_budget(9.0, gov) == 0.0  # depois do prazo = fim
    assert 0.0 <= explore_budget(0.7, gov) <= 1.0


# --- bench: sem KEEP em >= bench_after propostas -> banco ---


def test_bench_respeita_bench_after():
    gov = Governor(bench_after=3)
    stats = {
        "fria": {"proposals": 3, "keeps": 0},  # no limiar -> banco
        "quente": {"proposals": 5, "keeps": 1},  # tem KEEP -> fica
        "nova": {"proposals": 2, "keeps": 0},  # amostra curta -> fica
        "torta": {"proposals": "x", "keeps": None},  # dado quebrado -> fica
    }
    assert bench(stats, gov) == {"fria"}


def test_bench_vazio_nao_bane_ninguem():
    assert bench({}, Governor()) == set()


# --- enforcement no grafo: deadline estourado termina em escalate ---


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


def test_run_com_deadline_estourado_escala_sem_retry(data_dir, tmp_path, monkeypatch):
    """run_s minúsculo: o started_ts do plan já está "no passado" quando o gate
    decide. Verify vermelho pediria retry (budget de sobra), mas o governor
    corta: escalate na primeira tentativa, nenhum retry."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "governor.toml").write_text("[deadline]\nrun_s = 0.000001\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))

    unit = _unit(tmp_path, "prazo", "test -f nao_existe.txt")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-prazo", max_attempts=5)

    assert final["decision"].action == "escalate_human"
    assert "governor:prazo_estourado" in final["decision"].reason
    assert final["attempt"] == 0  # não houve nova tentativa
    nodes = [e["node"] for e in final["events"]]
    assert "retry" not in nodes
    assert "escalate" in nodes
    assert store.history()[0].ok is False


def test_run_sem_governor_toml_mantem_retry(data_dir, tmp_path, monkeypatch):
    """Config dir sem governor.toml = defaults fail-open: o retry acontece e a
    escalada é a de sempre (budget de tentativas), não a do prazo."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))

    unit = _unit(tmp_path, "solto", "test -f nao_existe.txt")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-solto", max_attempts=2)

    assert final["decision"].action == "escalate_human"
    assert "governor" not in final["decision"].reason
    assert final["attempt"] == 1  # o retry rodou
