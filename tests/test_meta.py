import math
from pathlib import Path

from harness.improve.meta import meta_check
from harness.ruler.gate import Decision, gate, kpi_regression_tolerance
from harness.types import Verdict

RULER = Path("config/ruler.toml")
PASSED = Verdict(passed=True, exit_code=0, log_path=Path("verify.log"), sec=1.0)


# --- meta_check: as 4 combinações ---


def test_meta_alvo_ruler_exame_falha_bloqueia():
    assert meta_check(RULER, lambda: False, human_ack=True) == "blocked"


def test_meta_alvo_ruler_exame_ok_sem_ack_quarentena():
    # autopilot nunca seta human_ack=True — este é o caminho dele.
    assert meta_check(RULER, lambda: True, human_ack=False) == "quarantined"


def test_meta_alvo_ruler_exame_ok_com_ack_permite():
    assert meta_check(Path("/abs/repo/config/ruler.toml"), lambda: True, human_ack=True) == "allowed"


def test_meta_alvo_fora_do_juiz_permite_sem_rodar_exame():
    chamado = []

    def exam() -> bool:
        chamado.append(True)
        return False

    assert meta_check(Path("prompts/coder.md"), exam, human_ack=False) == "allowed"
    assert not chamado


# --- gate: paridade sem ruler.toml e override que muda a decisão ---

BEFORE = {"x": 10.0}
WORSE_POUCO = {"x": 9.0}


def test_gate_sem_ruler_toml_e_comportamento_atual(tmp_path):
    ausente = tmp_path / "nao_existe.toml"
    assert kpi_regression_tolerance(ausente) == 0.0
    d = gate(PASSED, BEFORE, WORSE_POUCO, [], config_path=ausente)
    assert d == Decision("revert", "kpi_regression:x")


def test_gate_ruler_toml_malformado_e_comportamento_atual(tmp_path):
    quebrado = tmp_path / "ruler.toml"
    quebrado.write_text("isto nao é toml [[[", encoding="utf-8")
    assert kpi_regression_tolerance(quebrado) == 0.0
    assert gate(PASSED, BEFORE, WORSE_POUCO, [], config_path=quebrado).action == "revert"


def test_gate_config_real_do_repo_mantem_default_estrito():
    # o config/ruler.toml versionado tem que ser no-op: 0.0 = comportamento atual.
    assert kpi_regression_tolerance() == 0.0
    assert gate(PASSED, BEFORE, WORSE_POUCO, []).action == "revert"


def test_override_de_tolerancia_muda_decisao_do_gate(tmp_path):
    cfg = tmp_path / "ruler.toml"
    cfg.write_text("[gate]\nkpi_regression_tolerance = 2.0\n", encoding="utf-8")
    # piora de 1.0 <= tolerância 2.0 => accept; sem override seria revert.
    assert gate(PASSED, BEFORE, WORSE_POUCO, [], config_path=cfg).action == "accept"
    # piora de 5.0 > tolerância => revert continua.
    assert gate(PASSED, BEFORE, {"x": 5.0}, [], config_path=cfg).action == "revert"


def test_tolerancia_nunca_perdoa_kpi_sumido(tmp_path):
    cfg = tmp_path / "ruler.toml"
    cfg.write_text("[gate]\nkpi_regression_tolerance = 999.0\n", encoding="utf-8")
    assert gate(PASSED, BEFORE, {"x": math.nan}, [], config_path=cfg).action == "revert"
    assert gate(PASSED, BEFORE, {}, [], config_path=cfg).action == "revert"


def test_tolerancia_invalida_cai_no_default(tmp_path):
    cfg = tmp_path / "ruler.toml"
    cfg.write_text('[gate]\nkpi_regression_tolerance = "muita"\n', encoding="utf-8")
    assert kpi_regression_tolerance(cfg) == 0.0
    negativa = tmp_path / "neg.toml"
    negativa.write_text("[gate]\nkpi_regression_tolerance = -1.0\n", encoding="utf-8")
    assert kpi_regression_tolerance(negativa) == 0.0


# --- meta_check também guarda config/governor.toml (o chefe) ---

GOVERNOR = Path("config/governor.toml")


def test_meta_alvo_governor_exame_falha_bloqueia():
    assert meta_check(GOVERNOR, lambda: False, human_ack=True) == "blocked"


def test_meta_alvo_governor_exame_ok_sem_ack_quarentena():
    # o loop não afrouxa o próprio prazo: sem ack humano, não aplica.
    assert meta_check(GOVERNOR, lambda: True, human_ack=False) == "quarantined"


def test_meta_alvo_governor_exame_ok_com_ack_permite():
    abs_gov = Path("/abs/repo/config/governor.toml")
    assert meta_check(abs_gov, lambda: True, human_ack=True) == "allowed"


def test_meta_governor_fora_de_config_nao_e_guardado():
    chamado = []

    def exam() -> bool:
        chamado.append(True)
        return False

    assert meta_check(Path("outra/governor.toml"), exam, human_ack=False) == "allowed"
    assert not chamado
