"""Política de instalador da cerca: instalar no workspace sim, na máquina não.

O run precisa resolver dependência para fazer qualquer coisa útil (`uv sync`,
`npm ci`), então a cerca deixou de bloquear instalador em bloco. O que continua
recusado é o que escreve fora do workspace: flag global, cache compartilhado,
`pip` do sistema, venv apontado para fora, gerenciador da máquina.
"""

import pytest

pytest.importorskip("deepagents")

from harness.backends.safe_shell import check_command, installer_reason

PERMITIDOS = (
    "uv venv",
    "uv pip install -e .",
    "uv sync",
    "npm ci",
    "npm install --save-dev vitest",
    "pnpm install --frozen-lockfile",
    ".venv/bin/pip install -r requirements.txt",
    # `docker` em ARGUMENTO é leitura inocente: a cerca olha posição de comando.
    "cat docker-compose.yml",
    "grep image docker-compose.yml",
)

BLOQUEADOS = (
    "npm i -g typescript",
    "pip install --user x",
    "pip install --target /usr/local/lib x",
    "sudo npm ci",
    "pipx install x",
    "brew install node",
    "npm install --cache /Users/x/.npm",
    "uv venv /tmp/foo",
    "echo ok && npm i -g x",
    "docker compose up",
    "docker-compose up -d",
)


@pytest.mark.parametrize("cmd", PERMITIDOS)
def test_instalar_no_workspace_passa(cmd, tmp_path):
    assert check_command(cmd, tmp_path) is None, cmd


@pytest.mark.parametrize("cmd", BLOQUEADOS)
def test_instalar_fora_do_workspace_e_bloqueado(cmd, tmp_path):
    assert check_command(cmd, tmp_path) is not None, cmd


def test_motivo_de_instalador_ganha_do_motivo_genererico(tmp_path):
    """Motivo específico ensina mais que "caminho absoluto fora do workspace"."""
    motivo = check_command("pip install --target /usr/local/lib x", tmp_path)
    assert motivo is not None and "--target" in motivo


def test_pip_do_sistema_aponta_para_o_venv_do_workspace(tmp_path):
    motivo = installer_reason("pip install requests", tmp_path)
    assert motivo is not None and "venv do workspace" in motivo
    assert installer_reason("pip install --python .venv/bin/python requests", tmp_path) is None


def test_segmento_depois_do_operador_tambem_e_checado(tmp_path):
    for cmd in ("echo ok && npm i -g x", "ls; pnpm add -g eslint", "cat f | npm i -g y"):
        assert installer_reason(cmd, tmp_path) is not None, cmd


def test_shlex_quebrado_nao_derruba_a_checagem(tmp_path):
    """Fail-open com quote aberto — menos no caso grosseiro de `-g`."""
    assert installer_reason("echo 'aberto", tmp_path) is None
    assert installer_reason("echo 'aberto && npm i -g x", tmp_path) is not None
