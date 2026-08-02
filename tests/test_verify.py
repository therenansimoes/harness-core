from pathlib import Path

from harness.ruler.verify import LOG_REL, NOEXEC_EXIT, TIMEOUT_EXIT, run_verify
from harness.types import UnitSpec


def _unit(cmd: str) -> UnitSpec:
    return UnitSpec(id="u1", path=Path("."), prompt="faça", verify_cmd=cmd)


def test_verify_que_passa(tmp_path):
    (tmp_path / "alvo.txt").write_text("ok\n", encoding="utf-8")
    v = run_verify(_unit("test -f alvo.txt && echo verde"), tmp_path)
    assert v.passed is True
    assert v.exit_code == 0
    assert v.sec >= 0
    assert v.log_path == tmp_path / LOG_REL
    assert "verde" in v.log_path.read_text(encoding="utf-8")


def test_verify_que_falha_guarda_stderr(tmp_path):
    v = run_verify(_unit("echo faltou 1>&2; exit 2"), tmp_path)
    assert v.passed is False
    assert v.exit_code == 2
    assert "faltou" in v.log_path.read_text(encoding="utf-8")


def test_verify_roda_no_workspace(tmp_path):
    (tmp_path / "marca").write_text("", encoding="utf-8")
    assert run_verify(_unit("ls marca"), tmp_path).passed is True
    assert run_verify(_unit("ls marca"), tmp_path.parent).passed is False


def test_verify_timeout_vira_exit_code(tmp_path):
    v = run_verify(_unit("sleep 5"), tmp_path, timeout_s=0.3)
    assert v.passed is False
    assert v.exit_code == TIMEOUT_EXIT
    assert "timeout" in v.log_path.read_text(encoding="utf-8")


def test_verify_cria_diretorio_de_log(tmp_path):
    ws = tmp_path / "novo"
    ws.mkdir()
    v = run_verify(_unit("true"), ws)
    assert v.log_path.is_file()
    assert v.log_path.parent.name == ".harness"


def test_codigo_de_nao_executou_e_distinto():
    assert NOEXEC_EXIT != TIMEOUT_EXIT != 0
