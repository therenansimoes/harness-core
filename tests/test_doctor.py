"""`harness doctor`: o diagnóstico tem que acusar o que está quebrado e só isso.

Sandbox com o `config/` real do repo (é ele que o doctor promete validar) e
data própria. Nenhum backend é executado — doctor faz preflight, que é local e
determinístico por contrato.

A assimetria testada aqui é o ponto do comando: FALHA (coisa nossa quebrada)
derruba o exit code; aviso (backend indisponível, catálogo vazio) não. Doctor
que sai 1 porque o Ollama está desligado vira ruído que ninguém lê.
"""

import shutil
import stat
from pathlib import Path

import pytest

from harness import doctor

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    shutil.copytree(REPO_CONFIG, tmp_path / "config")
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    # O `_bootstrap` da CLI liga estas por setdefault; o teste que chama
    # `checks()` direto precisa do mesmo ambiente de um processo saudável.
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    return tmp_path


def status_of(name: str, result: list[doctor.Check]) -> str:
    return next(c.status for c in result if c.name == name)


def test_doctor_sandbox_limpo_nao_tem_falha(sandbox):
    result = doctor.checks()

    assert doctor.failures(result) == []
    for name in ("genome", "tracing", "msgpack", "config", "catalog", "data", "ledger"):
        assert status_of(name, result) == doctor.OK, name
    # todo backend registrado aparece, nem que seja como aviso
    assert [c for c in result if c.name.startswith("backend:")]
    assert status_of("backend:mock", result) == doctor.OK


def test_doctor_genoma_quebrado_falha(sandbox):
    """Genoma sem blocklist é genoma que não protege nada."""
    (sandbox / "config" / "genome.toml").write_text(
        'immutable = []\nmutable = ["config/*.toml"]\n', encoding="utf-8"
    )

    result = doctor.checks()

    assert status_of("genome", result) == doctor.FAIL
    assert [c.name for c in doctor.failures(result)] == ["genome"]
    # o arquivo é TOML válido: quem reprova é o genoma, não o parser
    assert status_of("config", result) == doctor.OK


def test_doctor_genoma_ausente_falha(sandbox):
    (sandbox / "config" / "genome.toml").unlink()

    assert status_of("genome", doctor.checks()) == doctor.FAIL


def test_doctor_toml_ilegivel_falha(sandbox):
    (sandbox / "config" / "kinds.toml").write_text("isto [ não = toml", encoding="utf-8")

    result = doctor.checks()

    assert status_of("config", result) == doctor.FAIL
    assert "kinds.toml" in next(c.detail for c in result if c.name == "config")


def test_doctor_catalogo_esteril_falha(sandbox):
    """`n_per_arm` abaixo do MIN_N: o catálogo carrega fechado, e doctor conta."""
    (sandbox / "config" / "catalog.toml").write_text(
        "[improve]\nn_per_arm = 3\n", encoding="utf-8"
    )

    assert status_of("catalog", doctor.checks()) == doctor.FAIL


def test_doctor_catalogo_sem_regra_e_aviso(sandbox):
    """Sem regra o improve não tem o que propor — mas o harness roda."""
    (sandbox / "config" / "catalog.toml").write_text(
        "[improve]\nn_per_arm = 6\n", encoding="utf-8"
    )

    result = doctor.checks()

    assert status_of("catalog", result) == doctor.WARN
    assert doctor.failures(result) == []


def test_doctor_tracing_ligado_falha(sandbox, monkeypatch):
    """Risco 2 da SPEC em forma de check: telemetria de terceiro é opt-in."""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    result = doctor.checks()

    assert status_of("tracing", result) == doctor.FAIL
    assert "LANGSMITH_TRACING" in next(c.detail for c in result if c.name == "tracing")


def test_doctor_endpoint_configurado_nao_e_tracing_ligado(sandbox, monkeypatch):
    """Var de endereço não é var de chave: só o token explícito conta como ON."""
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")

    assert status_of("tracing", doctor.checks()) == doctor.OK


def test_doctor_msgpack_frouxo_falha(sandbox, monkeypatch):
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "false")

    assert status_of("msgpack", doctor.checks()) == doctor.FAIL


def test_doctor_data_sem_permissao_falha(sandbox):
    data = sandbox / "data"
    data.mkdir()
    data.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = doctor.checks()
    finally:
        data.chmod(stat.S_IRWXU)

    assert status_of("data", result) == doctor.FAIL


def test_doctor_ledger_corrompido_falha(sandbox):
    data = sandbox / "data"
    data.mkdir()
    (data / "runs.sqlite").write_bytes(b"isto nao e um banco sqlite")

    assert status_of("ledger", doctor.checks()) == doctor.FAIL


# --- CLI ---------------------------------------------------------------------------


def test_cli_doctor_sai_0_e_imprime_uma_linha_por_check(sandbox, capsys):
    from harness import cli

    rc = cli.main(["doctor"])

    linhas = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert linhas[0].startswith("ok    genome ")
    assert linhas[-1].startswith("doctor checks=") and "falhas=0" in linhas[-1]
    assert len(linhas) == len(doctor.checks()) + 1


def test_cli_doctor_sai_1_com_genoma_quebrado(sandbox, capsys):
    from harness import cli

    (sandbox / "config" / "genome.toml").write_text("immutable = []\n", encoding="utf-8")

    rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "FALHA genome" in out
    assert "falhas=1" in out
