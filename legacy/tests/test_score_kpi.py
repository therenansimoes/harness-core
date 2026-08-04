#!/usr/bin/env python3
"""Testa a comparação A/B por KPI (D4b): score.kpi_report + gate de regressão.

Sem API e sem run: `kpi_report` é aritmética sobre a coluna `kpis` de linhas de
results.tsv. O que estes testes travam é o gate — KPI pior não pode passar como
"empate" só porque o success ficou igual.

    python3 -m pytest tests/test_score_kpi.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import kpi  # noqa: E402
import score  # noqa: E402


def _rows(values: list[dict | None], **row) -> list[dict]:
    """Linhas mínimas de results.tsv. `None` = linha SEM a coluna `kpis`
    (results.tsv anterior ao D4a)."""
    out = []
    for v in values:
        r = {
            "success": "1",
            "seconds": "1.0",
            "tokens": "100",
            "cost_usd": "0.01",
            "notes": "",
            **row,
        }
        if v is not None:
            r["kpis"] = kpi.to_json(v)
        out.append(r)
    return out


# ------------------------------------------------------------------ veredito


def test_kpi_melhor_nao_bloqueia():
    a = _rows([{"cobertura": 80.0}] * 4)
    b = _rows([{"cobertura": 92.0}] * 4)
    rep = score.kpi_report(a, b)
    assert rep["kpis"]["cobertura"]["verdict"] == score.BETTER
    assert rep["worse"] == [] and rep["blocked"] is False


def test_kpi_10pct_pior_com_direction_up_bloqueia():
    a = _rows([{"cobertura": 100.0}] * 4)
    b = _rows([{"cobertura": 90.0}] * 4)
    rep = score.kpi_report(a, b, {"cobertura": "up"})
    e = rep["kpis"]["cobertura"]
    assert e["verdict"] == score.WORSE
    assert e["delta"] == -0.10
    assert rep["worse"] == ["cobertura"] and rep["blocked"] is True


def test_direction_down_inverte_o_sinal():
    """Menos linhas é melhor: a MESMA queda de 10% vira BETTER."""
    a = _rows([{"linhas": 100.0}] * 4)
    b = _rows([{"linhas": 90.0}] * 4)
    assert score.kpi_report(a, b, {"linhas": "down"})["kpis"]["linhas"]["verdict"] == score.BETTER
    assert score.kpi_report(b, a, {"linhas": "down"})["kpis"]["linhas"]["verdict"] == score.WORSE


def test_default_direction_e_up():
    a = _rows([{"x": 100.0}] * 3)
    b = _rows([{"x": 80.0}] * 3)
    rep = score.kpi_report(a, b)  # sem directions
    assert rep["kpis"]["x"]["direction"] == kpi.DEFAULT_DIRECTION == "up"
    assert rep["kpis"]["x"]["verdict"] == score.WORSE


def test_variacao_abaixo_do_limiar_e_flat():
    a = _rows([{"x": 100.0}] * 4)
    b = _rows([{"x": 103.0}] * 4)  # +3% < 5%
    e = score.kpi_report(a, b)["kpis"]["x"]
    assert e["verdict"] == score.FLAT and "limiar" in e["reason"]


# ------------------------------------------------------- dados incompletos


def test_nan_e_ignorado_e_nao_conta_como_valor():
    """3 valores + 1 NaN em A: o NaN some da mediana e do n."""
    a = _rows([{"x": 10.0}, {"x": 10.0}, {"x": 10.0}, {"x": float("nan")}])
    b = _rows([{"x": 20.0}] * 3)
    e = score.kpi_report(a, b, {"x": "up"})["kpis"]["x"]
    assert e["n_a"] == 3 and e["n_b"] == 3
    assert e["median_a"] == 10.0
    assert e["verdict"] == score.BETTER


def test_nan_dos_dois_lados_nao_inventa_zero():
    a = _rows([{"x": float("nan")}] * 4)
    b = _rows([{"x": 5.0}] * 4)
    rep = score.kpi_report(a, b)
    assert rep["kpis"] == {}  # A não tem valor válido nenhum
    assert rep["only_b"] == ["x"] and rep["blocked"] is False


def test_kpi_so_de_um_lado_e_ignorado():
    a = _rows([{"comum": 10.0, "so_a": 1.0}] * 4)
    b = _rows([{"comum": 10.0, "so_b": 1.0}] * 4)
    rep = score.kpi_report(a, b)
    assert set(rep["kpis"]) == {"comum"}
    assert rep["only_a"] == ["so_a"] and rep["only_b"] == ["so_b"]
    assert rep["blocked"] is False


def test_menos_de_3_valores_validos_vira_flat_por_n():
    a = _rows([{"x": 100.0}] * 2)
    b = _rows([{"x": 10.0}] * 5)  # -90% seria WORSE, mas A tem n=2
    e = score.kpi_report(a, b)["kpis"]["x"]
    assert e["verdict"] == score.FLAT
    assert e["n_a"] == 2 and "insuficientes" in e["reason"]
    assert e["delta"] is None


def test_linha_sem_coluna_kpis_vale_dict_vazio():
    a = _rows([None] * 4)
    b = _rows([None] * 4)
    rep = score.kpi_report(a, b)
    assert rep["kpis"] == {} and rep["blocked"] is False
    # e mistura de linha velha com linha nova não quebra nem conta a velha
    a2 = _rows([None, None, {"x": 10.0}, {"x": 10.0}, {"x": 10.0}])
    assert score.kpi_report(a2, _rows([{"x": 10.0}] * 3))["kpis"]["x"]["n_a"] == 3


def test_json_quebrado_e_celula_vazia_nao_derrubam():
    a = [{"kpis": "{isso nao e json"}, {"kpis": ""}, *_rows([{"x": 1.0}] * 3)]
    b = _rows([{"x": 1.0}] * 3)
    assert score.kpi_report(a, b)["kpis"]["x"]["verdict"] == score.FLAT


def test_mediana_a_zero_nao_divide_por_zero():
    a = _rows([{"x": 0.0}] * 3)
    b = _rows([{"x": 5.0}] * 3)
    e = score.kpi_report(a, b)["kpis"]["x"]
    assert e["verdict"] == score.FLAT and e["delta"] is None


# ----------------------------------------------------------- gate do ab_report


def _ab_rows(version: str, n: int, success: int, kpi_value: float) -> list[dict]:
    return [
        {
            "harness_version": version,
            "suite": "s",
            "task_id": "t",
            "success": str(success),
            "seconds": "1.0",
            "tokens": "100",
            "cost_usd": "0.0100",
            "notes": "",
            "kpis": kpi.to_json({"cobertura": kpi_value}),
        }
        for _ in range(n)
    ]


def test_ab_report_bloqueia_merge_com_kpi_pior():
    rows = _ab_rows("va", 6, 1, 100.0) + _ab_rows("vb", 6, 1, 90.0)
    rep = score.ab_report(rows, "va", "vb")
    assert rep["kpi"]["worse"] == ["cobertura"]
    assert rep["merge"] is False
    assert any("regressão de KPI" in g for g in rep["failed"])


def test_ab_report_sem_kpi_pior_nao_falha_o_gate_de_kpi():
    rows = _ab_rows("va", 6, 1, 90.0) + _ab_rows("vb", 6, 1, 100.0)
    rep = score.ab_report(rows, "va", "vb")
    assert rep["kpi"]["blocked"] is False
    assert not any("KPI" in g for g in rep["failed"])


# ------------------------------------------------------- direction no kpi.toml


def test_load_directions_le_o_campo_direction(tmp_path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "kpi.toml").write_text(
        '[kpi.linhas]\ncmd = "echo 1"\ndirection = "down"\n\n'
        '[kpi.cobertura]\ncmd = "echo 2"\n\n'
        '[kpi.torto]\ncmd = "echo 3"\ndirection = "lateral"\n'
    )
    assert kpi.load_directions(tmp_path) == {
        "linhas": "down",
        "cobertura": "up",  # default
        "torto": "up",  # valor desconhecido cai no default (aviso no stderr)
    }


def test_load_directions_sem_kpi_toml_e_vazio(tmp_path):
    assert kpi.load_directions(tmp_path) == {}


# ------------------------------------------------------- decisão do experiment


def _exp_runs(kpi_a: float, kpi_b: float, n: int = 6) -> list[dict]:
    runs = []
    for i in range(n):
        for arm, v in (("A", kpi_a), ("B", kpi_b)):
            runs.append(
                {
                    "arm": arm,
                    "pair_index": i,
                    "success": 1,
                    "cost_usd": 0.01,
                    "tokens": 100,
                    "turns": 1,
                    "kpis": kpi.to_json({"cobertura": v}),
                }
            )
    return runs


def test_experiment_decide_bloqueia_por_kpi_regression():
    import experiment

    runs = _exp_runs(100.0, 85.0)
    agg = experiment.aggregate(runs)
    d = experiment.decide(agg, {}, runs)
    assert d["outcome"] == "rejeitar"
    assert d["blocked_by"] == ["kpi_regression:cobertura"]
    assert d["rule"] == "wilson+kpi"
    assert d["kpi"]["kpis"]["cobertura"]["verdict"] == score.WORSE


def test_experiment_decide_sem_kpi_mantem_o_wilson():
    import experiment

    runs = _exp_runs(100.0, 100.0)
    agg = experiment.aggregate(runs)
    d = experiment.decide(agg, {}, runs)
    assert d["kpi_blocked"] is False and d["blocked_by"] == []
    assert d["rule"] == "wilson"
    assert d["outcome"] == experiment._OUTCOME[d["verdict"]]
