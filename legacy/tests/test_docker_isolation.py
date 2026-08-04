#!/usr/bin/env python3
"""Prova o degrau "isolamento sério" (STATUS.md): verify.py rodando isolado
num container Docker descartável, --network none.

Skipa inteiro se docker não estiver instalado/rodando, em vez de fingir que
passou.

    python3 -m pytest tests/test_docker_isolation.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import isolation  # noqa: E402

TEST_IMAGE = "harness-runner:test"

pytestmark = pytest.mark.skipif(
    not isolation.docker_available(),
    reason="docker indisponível (binário ausente ou daemon parado)",
)


@pytest.fixture(scope="module", autouse=True)
def _built_image():
    """Builda a imagem de teste uma vez por sessão de teste do módulo."""
    isolation.ensure_image(TEST_IMAGE, timeout=300)
    yield


def test_verify_task_01_passa_dentro_do_container(tmp_path):
    """task_01 (README com seções) — verify roda dentro do container e passa
    quando o workspace já tem a solução."""
    ws = tmp_path / "ws"
    ws.mkdir()
    readme = "\n".join(
        [
            "# csvpeek",
            "",
            "Ferramenta CLI fictícia que imprime estatísticas de um CSV.",
            "",
            "## Instalação",
            "",
            "pip install csvpeek",
            "",
            "## Uso",
            "",
            "```",
            "csvpeek dados.csv",
            "```",
            "",
            "## Opções",
            "",
            "--rows, --cols, --json",
            "",
            "## Exemplo",
            "",
            "```",
            "linhas: 10",
            "```",
            "",
        ]
    )
    (ws / "README.md").write_text(readme)
    verify = REPO / "tasks" / "task_01" / "verify.py"

    r = isolation.run_verify_in_container(ws, verify, image=TEST_IMAGE)

    assert r.returncode == 0, f"verify falhou dentro do container: {r.stdout}\n{r.stderr}"


def test_network_none_bloqueia_rede(tmp_path):
    """Verify sintético que tenta curl num host externo: sem --network none
    isso teria chance de responder; com --network none tem que falhar sempre
    por erro de rede (sem interface, sem DNS)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    verify = tmp_path / "verify_net.py"
    verify.write_text(
        "import subprocess, sys\n"
        "r = subprocess.run(['curl', '-sS', '--max-time', '5', 'http://example.com'],"
        " capture_output=True)\n"
        "sys.exit(0 if r.returncode == 0 else 1)\n"
    )

    r = isolation.run_verify_in_container(ws, verify, image=TEST_IMAGE)

    assert r.returncode != 0, (
        f"verify sintético conseguiu rede com --network none (deveria falhar): "
        f"{r.stdout}\n{r.stderr}"
    )
