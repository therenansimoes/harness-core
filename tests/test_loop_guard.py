"""Testes da guarda de loop: repetição idêntica avisa uma vez, variação não avisa."""

import pytest

pytest.importorskip("langchain.agents.middleware")

from langchain_core.messages import ToolMessage

from harness.backends.loop_guard import LoopGuardMiddleware


class _Request:
    """Só o que o middleware lê do ToolCallRequest."""

    def __init__(self, name: str, args: dict):
        self.tool_call = {"name": name, "args": args, "id": "c1"}


def _chama(guard, name, args, corpo="ok"):
    pedido = _Request(name, args)
    return guard.wrap_tool_call(
        pedido,
        lambda req: ToolMessage(content=corpo, name=name, tool_call_id="c1"),
    )


def test_terceira_repeticao_avisa_e_quarta_nao_reavisa():
    guard = LoopGuardMiddleware()
    args = {"file_path": "/a.py", "old_string": "x", "new_string": "y"}

    assert "loop_guard" not in _chama(guard, "edit_file", args).content
    assert "loop_guard" not in _chama(guard, "edit_file", args).content
    terceira = _chama(guard, "edit_file", args).content
    assert "loop_guard" in terceira
    assert "edit_file" in terceira
    # Cooldown: a janela foi zerada, senão o aviso viraria eco a cada turno.
    assert "loop_guard" not in _chama(guard, "edit_file", args).content


def test_tool_diferente_no_meio_nao_conta_como_repeticao():
    guard = LoopGuardMiddleware()
    args = {"file_path": "/a.py", "old_string": "x", "new_string": "y"}

    _chama(guard, "edit_file", args)
    _chama(guard, "edit_file", args)
    assert "loop_guard" not in _chama(guard, "write_file", {"file_path": "/a.py"}).content


def test_guarda_entra_antes_do_retry_no_stack(tmp_path, monkeypatch):
    """Ordem é o contrato: mais externo = conta call do MODELO, não retry de infra."""
    pytest.importorskip("deepagents")
    import deepagents

    from harness.backends import deepagents_backend as da
    from harness.types import ExecRequest

    capturado: dict[str, object] = {}
    monkeypatch.setattr(
        deepagents,
        "create_deep_agent",
        lambda *a, **kw: capturado.setdefault("middleware", kw["middleware"]) and object(),
    )
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    nomes = [type(m).__name__ for m in capturado["middleware"]]
    assert nomes.index("LoopGuardMiddleware") < nomes.index("ToolRetryMiddleware")


def test_mesma_tool_com_argumento_diferente_nao_avisa():
    guard = LoopGuardMiddleware()
    for velho in ("um", "dois", "tres"):
        res = _chama(
            guard,
            "edit_file",
            {"file_path": "/a.py", "old_string": velho, "new_string": "y"},
        )
        assert "loop_guard" not in res.content
