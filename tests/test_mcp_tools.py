"""load_mcp_tools: todo caminho de falha vira [], nunca exceção."""

import sys
import types

from harness.backends.mcp_tools import load_mcp_tools

ENABLED = """\
[servers.foo]
enabled = true
transport = "stdio"
command = "x"
args = ["a"]
"""


def _write(tmp_path, text):
    p = tmp_path / "mcp.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file(tmp_path):
    assert load_mcp_tools(tmp_path / "nao_existe.toml") == []


def test_all_disabled(tmp_path):
    p = _write(tmp_path, '[servers.foo]\nenabled = false\ntransport = "stdio"\ncommand = "x"\n')
    assert load_mcp_tools(p) == []


def test_malformed_toml(tmp_path):
    p = _write(tmp_path, "isto não é toml [[[")
    assert load_mcp_tools(p) == []


def test_lib_missing(tmp_path, monkeypatch):
    # sys.modules[nome] = None força ImportError mesmo com o pacote instalado
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", None)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", None)
    assert load_mcp_tools(_write(tmp_path, ENABLED)) == []


def _fake_adapter(monkeypatch, client_cls):
    pkg = types.ModuleType("langchain_mcp_adapters")
    mod = types.ModuleType("langchain_mcp_adapters.client")
    mod.MultiServerMCPClient = client_cls
    pkg.client = mod
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", pkg)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", mod)


def test_fake_adapter_returns_tools(tmp_path, monkeypatch):
    ferramentas = ["t1", "t2"]
    vistas = {}

    class FakeClient:
        def __init__(self, connections):
            vistas["conn"] = connections

        async def get_tools(self):
            return ferramentas

    _fake_adapter(monkeypatch, FakeClient)
    assert load_mcp_tools(_write(tmp_path, ENABLED)) == ferramentas
    assert vistas["conn"] == {"foo": {"transport": "stdio", "command": "x", "args": ["a"]}}


def test_connection_error_returns_empty(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, connections):
            pass

        async def get_tools(self):
            raise ConnectionError("servidor fora do ar")

    _fake_adapter(monkeypatch, FakeClient)
    assert load_mcp_tools(_write(tmp_path, ENABLED)) == []
