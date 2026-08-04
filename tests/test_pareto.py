"""Pareto: o segundo filtro do A/B. Desligado, o repo é bit-a-bit o de antes."""

from harness.ruler import pareto
from harness.ruler.pareto import ParetoConfig

OFF = ParetoConfig(False, 0.10, 0.10)
ON = ParetoConfig(True, 0.10, 0.10)


def _axes(cost=None, sec=None) -> dict:
    return {"cost_usd": cost, "sec_total": sec}


# --- load_pareto ---------------------------------------------------------------


def test_config_ausente_vale_default(tmp_path):
    assert pareto.load_pareto(tmp_path / "nao_existe.toml") == pareto.DEFAULT_CONFIG


def test_toml_invalido_vale_default(tmp_path):
    p = tmp_path / "ruler.toml"
    p.write_text("[pareto\nenabled = ", encoding="utf-8")
    assert pareto.load_pareto(p) == pareto.DEFAULT_CONFIG


def test_enabled_so_liga_com_true_de_verdade(tmp_path):
    p = tmp_path / "ruler.toml"
    p.write_text('[pareto]\nenabled = "sim"\n', encoding="utf-8")
    assert pareto.load_pareto(p).enabled is False


def test_tolerancia_negativa_vale_default(tmp_path):
    p = tmp_path / "ruler.toml"
    p.write_text(
        '[pareto]\nenabled = true\ncost_tolerance_pct = -1.0\nsec_tolerance_pct = "muito"\n',
        encoding="utf-8",
    )
    cfg = pareto.load_pareto(p)
    assert cfg.enabled is True
    assert cfg.cost_tolerance_pct == pareto.PARETO_COST_TOL
    assert cfg.sec_tolerance_pct == pareto.PARETO_SEC_TOL


def test_toml_valido_e_lido(tmp_path):
    p = tmp_path / "ruler.toml"
    p.write_text(
        "[pareto]\nenabled = true\ncost_tolerance_pct = 0.25\nsec_tolerance_pct = 0.5\n",
        encoding="utf-8",
    )
    assert pareto.load_pareto(p) == ParetoConfig(True, 0.25, 0.5)


def test_config_real_do_repo_esta_desligada():
    # O default do repo é o comportamento histórico: só Wilson.
    assert pareto.load_pareto().enabled is False


# --- apply ---------------------------------------------------------------------


def test_desligado_nao_olha_eixo_nenhum():
    a, b = _axes(1.0, 10.0), _axes(3.0, 30.0)
    assert pareto.apply("KEEP", a, b, OFF) == ("KEEP", [])


def test_ligado_custo_acima_da_tolerancia_vira_inconclusive():
    a, b = _axes(1.0, 10.0), _axes(1.5, 10.0)
    assert pareto.apply("KEEP", a, b, ON) == ("INCONCLUSIVE", ["cost_usd"])


def test_ligado_custo_dentro_da_tolerancia_mantem_keep():
    a, b = _axes(1.0, 10.0), _axes(1.05, 10.0)
    assert pareto.apply("KEEP", a, b, ON) == ("KEEP", [])


def test_ligado_tempo_dobrado_vira_inconclusive():
    a, b = _axes(1.0, 10.0), _axes(1.0, 20.0)
    assert pareto.apply("KEEP", a, b, ON) == ("INCONCLUSIVE", ["sec_total"])


def test_ordem_dos_eixos_e_a_de_AXES():
    a, b = _axes(1.0, 10.0), _axes(9.0, 90.0)
    assert pareto.worse_axes(a, b, ON) == ["cost_usd", "sec_total"]


def test_discard_e_inconclusive_passam_intactos():
    a, b = _axes(1.0, 10.0), _axes(9.0, 90.0)
    assert pareto.apply("DISCARD", a, b, ON) == ("DISCARD", [])
    assert pareto.apply("INCONCLUSIVE", a, b, ON) == ("INCONCLUSIVE", [])


def test_fail_open_baseline_sem_medida_nao_bloqueia():
    # mock não cobra: custo None ou média 0.0 no braço A não tem contra o que
    # comparar, e Pareto não pode fechar o caminho de $0 do repo.
    assert pareto.apply("KEEP", _axes(None, 10.0), _axes(5.0, 10.0), ON) == ("KEEP", [])
    assert pareto.apply("KEEP", _axes(0.0, 10.0), _axes(5.0, 10.0), ON) == ("KEEP", [])
    assert pareto.apply("KEEP", _axes(1.0, 10.0), _axes(None, 10.0), ON) == ("KEEP", [])


# --- integração com o nó `score` ------------------------------------------------


def _state(sec_a: float, sec_b: float) -> dict:
    return {
        "arms": {"a": [2, 10], "b": [9, 10]},  # KEEP folgado no Wilson
        "axes": {
            "a": {"cost_usd": None, "sec_total": sec_a},
            "b": {"cost_usd": None, "sec_total": sec_b},
        },
    }


def test_score_com_pareto_ligado_derruba_keep_por_tempo(tmp_path, monkeypatch):
    from harness.graph import autopilot_graph

    p = tmp_path / "ruler.toml"
    p.write_text("[pareto]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(pareto, "RULER_CONFIG", p)

    out = autopilot_graph._score(_state(10.0, 20.0))
    assert out["verdict"] == "INCONCLUSIVE"
    assert out["events"][0]["pareto"] == "sec_total"


def test_score_com_config_real_mantem_keep():
    from harness.graph import autopilot_graph

    out = autopilot_graph._score(_state(10.0, 20.0))
    assert out["verdict"] == "KEEP"
    assert "pareto" not in out["events"][0]
