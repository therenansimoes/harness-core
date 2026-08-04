#!/usr/bin/env python3
"""Testa profile.py: detecção determinística de stack/comandos (D3).

Fixtures são repos de mentira montados em tmpdir — nada de rede, nada de
subprocess. As pegadinhas da pesquisa (stub do `npm init`, script sem
lockfile, Makefile sem alvo `test:`) têm teste próprio: é onde a detecção
erra na vida real.

    python3 -m pytest tests/test_profile.py -q
"""

from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import profile as profile_mod  # noqa: E402


def _mkrepo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    repo.mkdir(exist_ok=True)
    return repo


# ------------------------------------------------------------------ detecção


def test_python_pyproject_com_pytest(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "tests/test_x.py": "def test_x(): assert True\n",
        },
    )
    prof = profile_mod.detect(repo)
    assert prof.stack == "python-pytest"
    assert prof.test_cmd == "pytest"
    assert prof.matched_marker == "pyproject.toml [tool.pytest.ini_options]"


def test_node_com_script_e_lockfile(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "package.json": '{"name": "x", "scripts": {"test": "jest", "lint": "eslint ."}}',
            "package-lock.json": "{}",
        },
    )
    prof = profile_mod.detect(repo)
    assert prof.stack == "npm"
    assert prof.test_cmd == "npm test"
    assert prof.lint_cmd == "npm run lint"
    assert prof.build_cmd is None


def test_node_stub_do_npm_init_nao_conta(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "package.json": '{"name": "x", "scripts": '
            '{"test": "echo \\"Error: no test specified\\" && exit 1"}}',
            "package-lock.json": "{}",
        },
    )
    prof = profile_mod.detect(repo)
    assert prof.test_cmd is None
    assert prof.stack == "unknown"


def test_node_script_sem_lockfile_nao_conta(tmp_path):
    repo = _mkrepo(tmp_path, {"package.json": '{"name": "x", "scripts": {"test": "jest"}}'})
    assert profile_mod.detect(repo).test_cmd is None


def test_makefile_vence_linguagem(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "Makefile": "help:\n\t@echo hi\n\ntest:\n\tpytest -q\n",
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "tests/test_x.py": "def test_x(): assert True\n",
        },
    )
    prof = profile_mod.detect(repo)
    assert prof.stack == "make"
    assert prof.test_cmd == "make test"
    assert prof.lint_cmd is None


def test_makefile_sem_alvo_test_nao_conta(tmp_path):
    """Marcador fantasma: Makefile só com `docs:` não implica `make test`."""
    repo = _mkrepo(
        tmp_path,
        {
            "Makefile": "docs:\n\tsphinx-build docs out\n",
            "pyproject.toml": "[tool.pytest.ini_options]\n",
        },
    )
    assert profile_mod.detect(repo).test_cmd == "pytest"


def test_repo_vazio(tmp_path):
    repo = _mkrepo(tmp_path, {})
    prof = profile_mod.detect(repo)
    assert prof.stack == "unknown"
    assert (prof.test_cmd, prof.lint_cmd, prof.build_cmd) == (None, None, None)
    assert prof.conventions == ""


def test_uv_lock_vence_pytest_puro(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\n\n[tool.ruff]\nline-length = 100\n",
            "uv.lock": "version = 1\n",
        },
    )
    prof = profile_mod.detect(repo)
    assert prof.stack == "python-uv"
    assert prof.test_cmd == "uv run pytest"
    assert prof.lint_cmd == "uv run ruff check ."


def test_workspace_antes_de_linguagem(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n",
            "package.json": '{"scripts": {"test": "jest"}}',
            "pnpm-lock.yaml": "",
        },
    )
    prof = profile_mod.detect(repo)
    assert prof.stack == "pnpm-workspace"
    assert prof.test_cmd == "pnpm -r test"


def test_go_nunca_go_test_puro(tmp_path):
    repo = _mkrepo(tmp_path, {"go.mod": "module x\n"})
    prof = profile_mod.detect(repo)
    assert prof.test_cmd == "go test ./..."
    assert prof.build_cmd == "go build ./..."


def test_tests_dir_sem_config_escopa_no_diretorio(tmp_path):
    """Extensão nossa à tabela: sem config, pytest coletaria a árvore toda."""
    repo = _mkrepo(tmp_path, {"tests/test_x.py": "def test_x(): assert True\n"})
    prof = profile_mod.detect(repo)
    assert prof.stack == "python-pytest"
    assert prof.test_cmd == "pytest tests/"


def test_conventions_truncado(tmp_path):
    repo = _mkrepo(tmp_path, {"CLAUDE.md": "regra\n" + "x" * 5000})
    prof = profile_mod.detect(repo)
    assert prof.conventions.startswith("# CLAUDE.md\nregra")
    assert len(prof.conventions) <= profile_mod.CONVENTIONS_MAX + 20


# --------------------------------------------------------- persistência/cache


def test_write_e_load_profile(tmp_path):
    repo = _mkrepo(
        tmp_path,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "CLAUDE.md": 'aspas " e \\ barra\nquebra',
        },
    )
    out = profile_mod.write_profile(repo)
    assert out == repo / ".harness" / "profile.toml"

    data = tomllib.loads(out.read_text(encoding="utf-8"))["profile"]
    assert data["test_cmd"] == "pytest"
    assert data["conventions"].endswith("quebra")

    loaded = profile_mod.load_profile(repo)
    assert loaded == profile_mod.detect(repo)


def test_load_profile_redetecta_quando_velho(tmp_path):
    repo = _mkrepo(tmp_path, {"go.mod": "module x\n"})
    stale = profile_mod.to_toml(
        profile_mod.Profile(stack="rust", test_cmd="cargo test"),
        detected_at=time.time() - profile_mod.CACHE_TTL_S - 1,
    )
    (repo / ".harness").mkdir()
    (repo / ".harness" / "profile.toml").write_text(stale, encoding="utf-8")

    assert profile_mod.load_profile(repo).test_cmd == "go test ./..."


# ------------------------------------------------------- injeção no prompt


def test_prompt_block_vazio_para_unknown(tmp_path):
    assert profile_mod.prompt_block(profile_mod.detect(_mkrepo(tmp_path, {}))) == ""


def test_agent_injeta_bloco_no_system_prompt(tmp_path):
    import agent

    repo = _mkrepo(
        tmp_path,
        {
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "AGENTS.md": "commit em português",
        },
    )
    sp = agent._system_prompt(repo)
    assert sp.startswith(agent.SYSTEM_PROMPT)
    assert "stack python-pytest" in sp
    assert "Testes: `pytest`." in sp
    assert "commit em português" in sp
