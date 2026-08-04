"""Blocker tipado: o vocabulário é fechado e a declaração vence a inferência."""

from pathlib import Path

import pytest

from harness.backends import blocker_tools as bt
from harness.backends import deepagents_backend as da
from harness.types import ExecRequest


def _tool(ws: Path):
    pytest.importorskip("langchain_core")
    tools = bt.make_blocker_tools(ws)
    assert [t.name for t in tools] == ["declare_blocker"]
    return tools[0]


def test_tipo_invalido_nao_grava(tmp_path):
    out = _tool(tmp_path).invoke({"type": "preciso_de_cafe", "detail": "x"})
    assert "preciso_de_cafe" in out
    for tipo in bt.TYPES:
        assert tipo in out  # o modelo tem que saber o vocabulário para tentar de novo
    assert bt.read_blocker(tmp_path) is None
    assert not bt.blocker_path(tmp_path).exists()


def test_detail_vazio_nao_grava(tmp_path):
    out = _tool(tmp_path).invoke({"type": "needs_user_input", "detail": "   "})
    assert "detail" in out
    assert bt.read_blocker(tmp_path) is None


def test_declare_grava_sidecar_sem_deixar_tmp(tmp_path):
    out = _tool(tmp_path).invoke(
        {"type": "needs_user_input", "detail": "faltou a chave da API"}
    )
    assert "needs_user_input" in out
    assert bt.read_blocker(tmp_path) == ("needs_user_input", "faltou a chave da API")
    # tmp irmão + os.replace: nada de arquivo meio escrito sobrando.
    assert list((tmp_path / ".harness").glob("*.tmp")) == []


def test_sidecar_torto_ou_tipo_desconhecido_e_none(tmp_path):
    p = bt.blocker_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{isto nao e json", encoding="utf-8")
    assert bt.read_blocker(tmp_path) is None
    bt.write_blocker(tmp_path, "tipo_inventado", "x")  # escrita crua, sem a tool
    assert bt.read_blocker(tmp_path) is None


# --- backend: a declaração é o exit_reason ------------------------------------


class _FakeAgent:
    """Substitui o grafo do deepagents: `invoke` faz o que o teste mandar."""

    def __init__(self, efeito) -> None:
        self.efeito = efeito

    def invoke(self, payload, config):
        self.efeito()
        return {"messages": []}


@pytest.fixture
def backend_falso(monkeypatch):
    """`execute` sem deepagents e sem modelo: só o caminho de decisão do backend."""
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)

    def montar(efeito):
        monkeypatch.setattr(
            da, "_build_agent", lambda req: (_FakeAgent(efeito), object())
        )
        return da.DeepagentsBackend()

    return montar


def _req(ws: Path) -> ExecRequest:
    return ExecRequest(prompt="x", workspace=ws, trace_path=ws / "trace.jsonl")


def test_declarar_vence_stalled_no_backend(tmp_path, backend_falso):
    backend = backend_falso(
        lambda: bt.write_blocker(tmp_path, "external_wait", "o deploy do fornecedor")
    )
    res = backend.execute(_req(tmp_path))
    assert res.exit_reason == "blocker"
    assert res.ok is False
    assert res.blocker == "external_wait"


def test_blocker_da_tentativa_anterior_nao_vaza(tmp_path, backend_falso):
    bt.write_blocker(tmp_path, "needs_user_input", "sobra da tentativa passada")
    backend = backend_falso(lambda: None)  # esta tentativa não declarou nada
    res = backend.execute(_req(tmp_path))
    assert res.exit_reason == "stalled"
    assert res.blocker is None
    assert bt.read_blocker(tmp_path) is None
