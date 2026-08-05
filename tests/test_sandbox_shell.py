"""SandboxedShellBackend: cerca primeiro, embrulho depois."""

import pytest

pytest.importorskip("deepagents")

from harness.backends.safe_shell import EMPTY_OUTPUT, MAX_TIMEOUT
from harness.backends.sandbox_shell import SandboxedShellBackend


class _Stub:
    """Estratégia identidade: registra o que viu e devolve o comando intacto."""

    name = "stub"

    def __init__(self):
        self.calls = []

    def wrap(self, command):
        self.calls.append(command)
        return command


def test_cerca_roda_antes_do_embrulho(tmp_path):
    stub = _Stub()
    b = SandboxedShellBackend(root_dir=str(tmp_path), virtual_mode=True, sandbox=stub)
    r = b.execute("sudo ls")
    assert r.exit_code == 126
    assert "comando bloqueado" in r.output
    assert stub.calls == []  # o sandbox nunca viu o comando


def test_embrulho_e_aplicado(tmp_path):
    stub = _Stub()
    stub.wrap = lambda c: (stub.calls.append(c), "echo wrapped-marker")[1]
    b = SandboxedShellBackend(root_dir=str(tmp_path), virtual_mode=True, sandbox=stub)
    r = b.execute("echo original")
    assert "wrapped-marker" in r.output
    assert "original" not in r.output
    assert stub.calls == ["echo original"]


def test_timeout_com_teto_e_comando_embrulhado(tmp_path, monkeypatch):
    from deepagents.backends.local_shell import LocalShellBackend
    from deepagents.backends.protocol import ExecuteResponse

    capture = {}

    def fake(self, command, *, timeout=None):
        capture.update(command=command, timeout=timeout)
        return ExecuteResponse(output="x", exit_code=0, truncated=False)

    monkeypatch.setattr(LocalShellBackend, "execute", fake)
    stub = _Stub()
    b = SandboxedShellBackend(root_dir=str(tmp_path), virtual_mode=True, sandbox=stub)
    b.execute("echo hi", timeout=999)
    assert capture["timeout"] == MAX_TIMEOUT
    assert capture["command"] == "echo hi"  # identidade: embrulho não mudou nada


def test_sucesso_vazio_ganha_texto_explicito(tmp_path):
    stub = _Stub()
    b = SandboxedShellBackend(root_dir=str(tmp_path), virtual_mode=True, sandbox=stub)
    r = b.execute("true")
    assert r.output == EMPTY_OUTPUT
    assert r.exit_code == 0
