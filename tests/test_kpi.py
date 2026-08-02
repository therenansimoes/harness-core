import math

import pytest

from harness.ruler.kpi import KpiSpec, collect, load_kpis, parse_value, regressed


@pytest.fixture
def repo(tmp_path):
    return tmp_path


def _write_kpis(repo, body: str):
    (repo / "kpis.toml").write_text(body, encoding="utf-8")


def test_sem_arquivo_nao_quebra_a_run(repo):
    assert load_kpis(repo) == {}
    assert collect(repo) == {}


def test_load_defaults_e_direction(repo):
    _write_kpis(repo, """
[kpi.testes]
cmd = "echo 3"

[kpi.linhas]
cmd = "echo 10"
direction = "lower"
timeout_s = 5
""")
    specs = load_kpis(repo)
    assert set(specs) == {"testes", "linhas"}
    assert specs["testes"] == KpiSpec("testes", "echo 3", "higher", 60.0)
    assert specs["linhas"] == KpiSpec("linhas", "echo 10", "lower", 5.0)


def test_load_ignora_entrada_sem_cmd_e_direction_invalida(repo, capsys):
    _write_kpis(repo, """
[kpi.vazio]
why = "sem cmd"

[kpi.torto]
cmd = "echo 1"
direction = "maior"
""")
    specs = load_kpis(repo)
    assert set(specs) == {"torto"}
    assert specs["torto"].direction == "higher"
    err = capsys.readouterr().err
    assert "vazio" in err and "torto" in err


def test_load_toml_quebrado_vale_vazio(repo, capsys):
    _write_kpis(repo, "[kpi.x\ncmd = ")
    assert load_kpis(repo) == {}
    assert "inválido" in capsys.readouterr().err


def test_parse_value_pega_a_ultima_linha():
    assert parse_value("ruído\n42\n") == 42.0
    assert parse_value("3.5") == 3.5
    assert math.isnan(parse_value(""))
    assert math.isnan(parse_value("passou\n"))


def test_collect_roda_no_repo_e_parseia(repo):
    (repo / "src.txt").write_text("a\nb\nc\n", encoding="utf-8")
    _write_kpis(repo, """
[kpi.linhas]
cmd = "wc -l < src.txt"

[kpi.ultima_linha]
cmd = "echo blá; echo 7"
""")
    values = collect(repo)
    assert values == {"linhas": 3.0, "ultima_linha": 7.0}


def test_collect_falha_vira_nan_registrado(repo, capsys):
    _write_kpis(repo, """
[kpi.quebrado]
cmd = "exit 3"

[kpi.sem_numero]
cmd = "echo tudo certo"
""")
    values = collect(repo)
    assert math.isnan(values["quebrado"])
    assert math.isnan(values["sem_numero"])
    err = capsys.readouterr().err
    assert "quebrado" in err and "sem_numero" in err


def test_collect_timeout_vira_nan(repo):
    _write_kpis(repo, """
[kpi.lento]
cmd = "sleep 5; echo 1"
""")
    values = collect(repo, timeout_s=0.3)
    assert math.isnan(values["lento"])


SPECS = {
    "testes": KpiSpec("testes", "x", "higher"),
    "linhas": KpiSpec("linhas", "x", "lower"),
}


def test_regressed_direction_higher():
    assert regressed({"testes": 10}, {"testes": 9}, SPECS) == ["testes"]
    assert regressed({"testes": 10}, {"testes": 11}, SPECS) == []
    assert regressed({"testes": 10}, {"testes": 10}, SPECS) == []


def test_regressed_direction_lower():
    assert regressed({"linhas": 100}, {"linhas": 120}, SPECS) == ["linhas"]
    assert regressed({"linhas": 100}, {"linhas": 80}, SPECS) == []


def test_regressed_sem_spec_assume_higher():
    assert regressed({"x": 5}, {"x": 4}, None) == ["x"]
    assert regressed({"x": 5}, {"x": 6}, {}) == []


def test_regressed_nan_depois_e_regressao():
    # perder a medição é o jeito barato de burlar o gate.
    assert regressed({"testes": 10}, {"testes": math.nan}, SPECS) == ["testes"]


def test_regressed_nan_antes_e_ignorado():
    assert regressed({"testes": math.nan}, {"testes": 1}, SPECS) == []
    assert regressed({"testes": math.nan}, {"testes": math.nan}, SPECS) == []


def test_regressed_ausente_em_before_e_ignorado():
    assert regressed({}, {"testes": 0}, SPECS) == []
    assert regressed({"testes": 10}, {}, SPECS) == []


def test_regressed_lista_ordenada_de_todos_os_piores():
    before = {"testes": 10, "linhas": 100, "ok": 1}
    after = {"testes": 8, "linhas": 130, "ok": 1}
    assert regressed(before, after, SPECS) == ["linhas", "testes"]
