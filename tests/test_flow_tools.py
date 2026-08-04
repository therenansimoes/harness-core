"""Tools de fluxo. Os parsers e o `detect_stack` rodam offline e sem subprocess.

O que se verifica aqui é a LEITURA: dado o log de um pytest/vitest que falhou,
a tool tem que devolver arquivo, linha, nome do teste e mensagem — é isso que o
modelo recebe em vez de 400 linhas de saída. `install_deps` de verdade fica sob
`-m slow` (baixa pacote, leva minutos).
"""

import json
from pathlib import Path

import pytest

from harness.backends import flow_tools

FIXTURES = Path(__file__).parent / "fixtures"

PYTEST_OUT = (FIXTURES / "pytest_fail.txt").read_text(encoding="utf-8")
VITEST_OUT = (FIXTURES / "vitest_fail.txt").read_text(encoding="utf-8")
NPM_ERR = (FIXTURES / "npm_err.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- parsers


def test_parse_pytest_conta_e_localiza():
    dados = flow_tools.parse_pytest(PYTEST_OUT)
    assert (dados["passed"], dados["failed"], dados["errors"]) == (4, 2, 0)
    assert [f["test"] for f in dados["failures"]] == ["test_soma", "test_divide"]
    primeiro = dados["failures"][0]
    assert primeiro["file"] == "tests/test_calc.py"
    assert primeiro["line"] == 19  # última linha de traceback vista no arquivo
    assert primeiro["msg"] == "assert 4 == 5"


def test_parse_pytest_verde():
    dados = flow_tools.parse_pytest("....\n6 passed in 0.10s\n")
    assert dados == {"passed": 6, "failed": 0, "errors": 0, "failures": []}


def test_parse_pytest_teto_de_falhas():
    linhas = "\n".join(f"FAILED tests/t.py::test_{i} - boom" for i in range(20))
    dados = flow_tools.parse_pytest(f"{linhas}\n20 failed in 1.0s\n")
    assert dados["failed"] == 20
    assert len(dados["failures"]) == flow_tools.MAX_FALHAS


def test_parse_pytest_corta_mensagem():
    longa = "x" * 500
    dados = flow_tools.parse_pytest(f"FAILED a.py::test_x - {longa}\n1 failed in 0.1s\n")
    assert len(dados["failures"][0]["msg"]) == flow_tools.MAX_MSG_CHARS


def test_parse_jest_conta_e_localiza():
    dados = flow_tools.parse_jest(VITEST_OUT)
    assert dados["failed"] == 1
    assert dados["passed"] == 4
    falha = next(f for f in dados["failures"] if f["line"])
    assert falha["file"] == "src/soma.test.ts"
    assert falha["line"] == 7
    assert "expected 4 to be 5" in falha["msg"]


def test_parse_jest_verde():
    dados = flow_tools.parse_jest(" Test Files  2 passed (2)\n      Tests  7 passed (7)\n")
    assert dados["failed"] == 0
    assert dados["passed"] == 7
    assert dados["failures"] == []


# --------------------------------------------------------------------------- erro filtrado


def test_erro_filtrado_pega_npm_error_e_respeita_teto():
    trecho = flow_tools._erro_filtrado(NPM_ERR)
    assert "npm error code EUSAGE" in trecho
    assert "npm warn config production" not in trecho  # warning não é erro
    assert len(trecho.splitlines()) <= flow_tools.MAX_ERRO_LINHAS
    assert len(trecho) <= flow_tools.MAX_ERRO_CHARS


def test_lock_ruim_reconhece_fallback():
    assert flow_tools._LOCK_RUIM.search(NPM_ERR)
    assert not flow_tools._LOCK_RUIM.search("npm error code ENOENT\nnpm error missing script")


# --------------------------------------------------------------------------- detect_stack


def test_detect_stack_vazio(tmp_path):
    stack = flow_tools.detect_stack(tmp_path)
    assert stack["python"] is False
    assert stack["node"] is False
    assert stack["lockfiles"] == []


def test_detect_stack_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    stack = flow_tools.detect_stack(tmp_path)
    assert stack["python"] and stack["pyproject"]
    assert stack["node"] is False
    assert stack["venv"] is False
    assert "uv.lock" in stack["lockfiles"]


def test_detect_stack_requirements_e_venv(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    binario = tmp_path / ".venv" / "bin" / "python"
    binario.parent.mkdir(parents=True)
    binario.touch()
    stack = flow_tools.detect_stack(tmp_path)
    assert stack["python"] and stack["requirements"] and stack["venv"]
    assert stack["pyproject"] is False


@pytest.mark.parametrize(
    "lock,esperado",
    [("package-lock.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn")],
)
def test_detect_stack_gerenciador_por_lockfile(tmp_path, lock, esperado):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "app", "scripts": {"test": "vitest run", "dev": "vite"}}),
        encoding="utf-8",
    )
    (tmp_path / lock).write_text("", encoding="utf-8")
    stack = flow_tools.detect_stack(tmp_path)
    assert stack["node"] is True
    assert stack["gerenciador_node"] == esperado
    assert stack["test_script"] == "vitest run"
    assert stack["scripts"]["dev"] == "vite"


def test_detect_stack_package_json_quebrado(tmp_path):
    (tmp_path / "package.json").write_text("{nao é json", encoding="utf-8")
    stack = flow_tools.detect_stack(tmp_path)
    assert stack["node"] is True  # o arquivo existe: a stack É node
    assert stack["scripts"] == {} and stack["test_script"] is None


# --------------------------------------------------------------------------- env


def test_env_aponta_venv_e_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "dados"))
    env = flow_tools._env(tmp_path / "ws")
    assert env["VIRTUAL_ENV"] == str(tmp_path / "ws" / ".venv")
    assert env["PATH"].startswith(str(tmp_path / "ws" / ".venv" / "bin"))
    assert env["npm_config_fund"] == "false" and env["npm_config_audit"] == "false"
    assert env["CI"] == "1" and env["NO_COLOR"] == "1"
    assert Path(env["UV_CACHE_DIR"]).is_dir()
    assert Path(env["npm_config_cache"]).is_dir()


# --------------------------------------------------------------------------- run_tests


def test_run_tests_sem_suite(tmp_path):
    assert "nenhuma suíte" in flow_tools.run_tests(tmp_path)


def test_run_tests_grava_log_e_resume(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    class Falso:
        returncode = 1
        stdout = PYTEST_OUT
        stderr = ""

    monkeypatch.setattr(flow_tools, "_run", lambda *a, **k: Falso())
    saida = flow_tools.run_tests(tmp_path)
    assert "ok=false" in saida and "passed=4 failed=2" in saida
    assert "tests/test_calc.py:19 test_soma: assert 4 == 5" in saida
    log = tmp_path / flow_tools.TESTS_LOG
    assert log.is_file() and "short test summary" in log.read_text(encoding="utf-8")
    assert f"log=/{flow_tools.TESTS_LOG}" in saida


def test_run_tests_bloqueio_da_cerca_volta_como_texto(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    def recusa(*a, **k):
        raise flow_tools.Blocked("bloqueado pela cerca: teste")

    monkeypatch.setattr(flow_tools, "_run", recusa)
    saida = flow_tools.run_tests(tmp_path)
    assert saida.startswith("run_tests: bloqueado pela cerca")


# --------------------------------------------------------------------------- lint


def test_run_lint_sem_stack(tmp_path):
    assert flow_tools._LINT_ALVO in flow_tools.run_lint(tmp_path)


def test_run_lint_sem_linter_resolvivel(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setattr(flow_tools, "_ruff_argv", lambda ws: None)
    assert flow_tools.run_lint(tmp_path) == f"ruff {flow_tools._LINT_ALVO}"


def test_run_lint_lista_dez_primeiros(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    achados = "\n".join(f"a.py:{i}:1: F401 unused import" for i in range(1, 31))

    class Falso:
        returncode = 1
        stdout = achados
        stderr = ""

    monkeypatch.setattr(flow_tools, "_ruff_argv", lambda ws: ["ruff"])
    monkeypatch.setattr(flow_tools, "_run", lambda *a, **k: Falso())
    saida = flow_tools.run_lint(tmp_path)
    assert saida.splitlines()[0] == "ruff ok=false 30 erros"
    assert len(saida.splitlines()) == flow_tools.MAX_LINT_LINHAS + 1


def test_cerca_nao_bloqueia_binario_do_harness(tmp_path):
    """O ruff/python do venv DO HARNESS é absoluto e fora do ws — por construção.

    Se ele passasse pela cerca como argv[0], toda chamada de lint em workspace
    de terceiro morreria em "caminho absoluto fora do workspace".
    """
    pytest.importorskip("deepagents")
    argv = ["/usr/local/bin/python", "-m", "ruff", "check", "."]
    assert flow_tools._do_harness(argv[0], tmp_path) is True
    assert flow_tools._cerca(argv, tmp_path, cerca_argv0=True) is not None
    assert flow_tools._cerca(argv, tmp_path, cerca_argv0=False) is None


def test_cerca_fecha_para_comando_do_modelo(tmp_path):
    """Argumento continua cercado mesmo com argv[0] liberado."""
    pytest.importorskip("deepagents")
    argv = ["/usr/local/bin/python", "-m", "pytest", "/etc/passwd"]
    assert flow_tools._cerca(argv, tmp_path, cerca_argv0=False) is not None
    assert flow_tools._do_harness(".venv/bin/ruff", tmp_path) is False


def test_run_lint_ruff_de_verdade_no_repo():
    """O ruff tem que estar resolvível no venv do harness (dependency-group dev)."""
    assert flow_tools._ruff_argv(Path(".")) is not None


# --------------------------------------------------------------------------- screenshot


def test_local_screenshot_recusa_porta_nao_registrada(tmp_path):
    saida = flow_tools.local_screenshot(5173, workspace=tmp_path)
    assert "não está registrada" in saida
    assert "nenhuma" in saida


def test_local_screenshot_usa_porta_registrada(tmp_path, monkeypatch):
    procs = tmp_path / flow_tools.PROCS_JSON
    procs.parent.mkdir(parents=True)
    procs.write_text(
        json.dumps([{"id": "p1", "pid": 1234, "port": 5173}]),
        encoding="utf-8",
    )
    assert flow_tools._portas_registradas(tmp_path) == {5173}

    from harness import uiverify

    chamadas = {}

    def falso_shot(url, out, timeout_s=None):
        chamadas["url"] = url  # None = deu certo, é o contrato do uiverify.screenshot
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"png")

    monkeypatch.setattr(uiverify, "screenshot", falso_shot)
    saida = flow_tools.local_screenshot(5173, "/login", "shot.png", tmp_path)
    assert chamadas["url"] == "http://127.0.0.1:5173/login"
    assert saida.startswith("ok=true") and "bytes=3" in saida


def test_local_screenshot_destino_fora_do_workspace(tmp_path, monkeypatch):
    procs = tmp_path / flow_tools.PROCS_JSON
    procs.parent.mkdir(parents=True)
    procs.write_text(json.dumps([{"id": "p1", "pid": 1, "port": 8080}]), encoding="utf-8")
    saida = flow_tools.local_screenshot(8080, "/", "../fora.png", tmp_path)
    assert "sai do workspace" in saida


# --------------------------------------------------------------------------- carga


def test_load_flow_tools_nomes():
    pytest.importorskip("langchain_core")
    nomes = [t.name for t in flow_tools.load_flow_tools(".")]
    assert nomes == [
        "install_deps",
        "run_tests",
        "run_lint",
        "local_screenshot",
        "detect_stack",
    ]


def test_load_flow_tools_fail_open(monkeypatch, capsys):
    """Falha na carga é `[]`, nunca exceção: o executor sobe sem as tools de fluxo."""
    import builtins

    real = builtins.__import__

    def sem_langchain(nome, *args, **kwargs):
        if nome.startswith("langchain"):
            raise ImportError("sem langchain neste ambiente")
        return real(nome, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", sem_langchain)
    assert flow_tools.load_flow_tools(".") == []
    assert "falha ao carregar tools de fluxo" in capsys.readouterr().err


def test_tools_documentadas_no_manual():
    manual = Path("prompts/tools.d/40-flows.md").read_text(encoding="utf-8")
    for nome in ("install_deps", "run_tests", "run_lint", "local_screenshot", "detect_stack"):
        assert f"## {nome}" in manual
        assert f"{nome}(" in manual  # todo verbete tem exemplo de chamada


# --------------------------------------------------------------------------- install (slow)


def test_install_deps_sem_manifesto(tmp_path):
    assert "nenhum manifesto" in flow_tools.install_deps(tmp_path)


def test_install_deps_audita_delta(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("nada\n", encoding="utf-8")
    binario = tmp_path / ".venv" / "bin" / "python"
    binario.parent.mkdir(parents=True)
    binario.touch()

    class Falso:
        returncode = 0
        stdout = ""
        stderr = ""

    freezes = iter([["a==1"], ["a==1", "b==2"]])  # antes, depois
    monkeypatch.setattr(flow_tools, "_run", lambda *a, **k: Falso())
    monkeypatch.setattr(flow_tools, "_pacotes_python", lambda ws: next(freezes))
    saida = flow_tools.install_deps(tmp_path)
    assert "ok=true gerenciador=uv pacotes=2 novos=1" in saida
    auditoria = (tmp_path / flow_tools.INSTALL_AUDIT).read_text(encoding="utf-8")
    assert "python\tantes=1\tdepois=2" in auditoria
    assert "\t+b==2" in auditoria


@pytest.mark.slow
def test_install_deps_python_de_verdade(tmp_path):
    """Rede + minutos: `-m slow`. Prova que o venv e o install acontecem mesmo."""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    saida = flow_tools.install_deps(tmp_path)
    assert "ok=true gerenciador=uv" in saida, saida
    assert (tmp_path / ".venv" / "bin" / "python").is_file()
