"""Empty-turn middleware: silence without tools triggers one nudged retry."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

pytest.importorskip("langchain.agents.middleware")

from harness.backends.empty_turn import EmptyTurnMiddleware, NUDGE, _is_empty_stop


class _Resp:
    def __init__(self, msg):
        self.result = [msg]


def test_nudge_names_procedure_and_write_file():
    assert "proc-recover-after-empty-turn" in NUDGE
    assert "write_file" in NUDGE


def test_empty_ai_without_tools_is_empty_stop():
    assert _is_empty_stop(_Resp(AIMessage(content="", tool_calls=[])))
    assert not _is_empty_stop(_Resp(AIMessage(content="ok", tool_calls=[])))
    assert not _is_empty_stop(
        _Resp(AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "1"}]))
    )


def test_wrap_retries_once_with_nudge():
    guard = EmptyTurnMiddleware()
    calls = {"n": 0}

    class _Req:
        messages = [HumanMessage(content="task")]

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(AIMessage(content=""))
        # second call must see nudge with the procedure name
        assert any(
            isinstance(m, HumanMessage) and "proc-recover-after-empty-turn" in m.content
            for m in req.messages
        )
        return _Resp(AIMessage(content="", tool_calls=[{"name": "write_file", "args": {}, "id": "2"}]))

    out = guard.wrap_model_call(_Req(), handler)
    assert calls["n"] == 2
    assert not _is_empty_stop(out)


def test_empty_turn_entra_no_stack(tmp_path, monkeypatch):
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
    assert "EmptyTurnMiddleware" in nomes
