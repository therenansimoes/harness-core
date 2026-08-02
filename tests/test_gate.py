import math
from pathlib import Path

import pytest

from harness.ruler.gate import Decision, gate
from harness.ruler.kpi import KpiSpec
from harness.types import Verdict

PASSED = Verdict(passed=True, exit_code=0, log_path=Path("verify.log"), sec=1.0)
FAILED = Verdict(passed=False, exit_code=2, log_path=Path("verify.log"), sec=1.0)

SPECS = {
    "testes": KpiSpec("testes", "x", "higher"),
    "linhas": KpiSpec("linhas", "x", "lower"),
}
BEFORE = {"testes": 10.0, "linhas": 100.0}


def test_accept_quando_verify_passa_e_kpi_nao_piora():
    d = gate(PASSED, BEFORE, {"testes": 12.0, "linhas": 90.0}, [], SPECS)
    assert d == Decision("accept", "verify ok, sem regressão de KPI")


def test_retry_quando_verify_falha():
    d = gate(FAILED, BEFORE, BEFORE, [], SPECS)
    assert d.action == "retry"
    assert "exit=2" in d.reason


def test_revert_quando_kpi_regride():
    # aceite do PR-4: verde no verify não compra regressão de KPI.
    d = gate(PASSED, BEFORE, {"testes": 8.0, "linhas": 100.0}, [], SPECS)
    assert d.action == "revert"
    assert d.reason == "kpi_regression:testes"


def test_revert_quando_kpi_some():
    d = gate(PASSED, BEFORE, {"testes": math.nan, "linhas": 100.0}, [], SPECS)
    assert d == Decision("revert", "kpi_regression:testes")


def test_revert_quando_ha_tamper():
    d = gate(PASSED, BEFORE, BEFORE, ["tamper:genome_violation"], SPECS)
    assert d.action == "revert"
    assert d.reason == "tamper:genome_violation"


def test_tamper_ganha_de_verify_e_de_kpi():
    d = gate(FAILED, BEFORE, {"testes": 0.0}, ["genome_violation", "tamper:sealed_touched"], SPECS)
    assert d == Decision("revert", "tamper:genome_violation,sealed_touched")


def test_verify_falho_ganha_de_kpi():
    d = gate(FAILED, BEFORE, {"testes": 1.0, "linhas": 999.0}, [], SPECS)
    assert d.action == "retry"


def test_sem_specs_usa_direction_default():
    assert gate(PASSED, {"x": 5.0}, {"x": 4.0}, []).action == "revert"
    assert gate(PASSED, {"x": 5.0}, {"x": 6.0}, []).action == "accept"


def test_sem_kpi_declarado_e_accept():
    assert gate(PASSED, {}, {}, []).action == "accept"


def test_decision_e_frozen():
    with pytest.raises(Exception):
        gate(PASSED, {}, {}, []).action = "revert"
