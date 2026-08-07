"""Tests for ToolSalvageMiddleware — Hermes tag + bare JSON rescue."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

pytest.importorskip("langchain.agents.middleware")

from harness.backends.tool_salvage import (
    ToolSalvageMiddleware,
    _normalize,
    _parse_calls,
)


# ---------------------------------------------------------------------------
# _parse_calls — unit
# ---------------------------------------------------------------------------


def test_parse_hermes_tag():
    text = '<tool_call>\n{"name": "write_file", "arguments": {"path": "x.py"}}\n</tool_call>'
    calls = _parse_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "write_file"
    assert calls[0]["args"] == {"path": "x.py"}


def test_parse_bare_json():
    text = 'Sure!\n{"name": "read_file", "arguments": {"path": "y.py"}}'
    calls = _parse_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"


def test_parse_args_style():
    """Accept 'args' key in addition to 'arguments'."""
    text = '{"name": "edit_file", "args": {"path": "z.py", "content": "x=1"}}'
    calls = _parse_calls(text)
    assert len(calls) == 1
    assert calls[0]["args"]["path"] == "z.py"


def test_parse_multiple_hermes_tags():
    text = (
        '<tool_call>{"name": "write_file", "arguments": {}}</tool_call> '
        '<tool_call>{"name": "read_file", "arguments": {}}</tool_call>'
    )
    calls = _parse_calls(text)
    assert len(calls) == 2


def test_normalize_rejects_invalid_name():
    assert _normalize({"name": "bad name!", "arguments": {}}) is None
    assert _normalize({"name": "", "arguments": {}}) is None
    assert _normalize({"name": "ok_name", "arguments": {}}) is not None


# ---------------------------------------------------------------------------
# ToolSalvageMiddleware — sync
# ---------------------------------------------------------------------------


def _make_resp(content="", tool_calls=None):
    ai = AIMessage(content=content, tool_calls=tool_calls or [])
    resp = MagicMock()
    resp.result = [ai]
    return resp


def test_rescues_hermes_content():
    mw = ToolSalvageMiddleware()
    content = '<tool_call>\n{"name": "write_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    resp = _make_resp(content=content)
    handler = MagicMock(return_value=resp)
    req = MagicMock()

    out = mw.wrap_model_call(req, handler)

    ai = out.result[-1]
    assert ai.tool_calls
    assert ai.tool_calls[0]["name"] == "write_file"


def test_no_op_when_tool_calls_present():
    mw = ToolSalvageMiddleware()
    tc = [{"name": "read_file", "args": {}, "id": "x", "type": "tool_call"}]
    resp = _make_resp(tool_calls=tc)
    handler = MagicMock(return_value=resp)
    req = MagicMock()

    out = mw.wrap_model_call(req, handler)

    assert out.result[-1].tool_calls == tc  # unchanged


def test_no_op_when_no_tool_in_content():
    mw = ToolSalvageMiddleware()
    resp = _make_resp(content="Just a plain response.")
    handler = MagicMock(return_value=resp)
    req = MagicMock()

    out = mw.wrap_model_call(req, handler)

    assert not out.result[-1].tool_calls


# ---------------------------------------------------------------------------
# ToolSalvageMiddleware — async
# ---------------------------------------------------------------------------


def test_async_rescues_hermes_content():
    mw = ToolSalvageMiddleware()
    content = '<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>'
    resp = _make_resp(content=content)

    async def handler(req):
        return resp

    out = asyncio.run(mw.awrap_model_call(MagicMock(), handler))
    assert out.result[-1].tool_calls[0]["name"] == "list_dir"


def test_tool_salvage_in_stack(tmp_path, monkeypatch):
    """ToolSalvageMiddleware appears in the deepagents middleware stack."""
    pytest.importorskip("deepagents")
    import deepagents

    from harness.backends import deepagents_backend as da
    from harness.types import ExecRequest

    captured: dict = {}
    monkeypatch.setattr(
        deepagents,
        "create_deep_agent",
        lambda *a, **kw: captured.setdefault("middleware", kw["middleware"]) and object(),
    )
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    names = [type(m).__name__ for m in captured["middleware"]]
    assert "ToolSalvageMiddleware" in names


def test_stack_order_salvage_before_empty_turn(tmp_path, monkeypatch):
    """ToolSalvageMiddleware must appear immediately before EmptyTurnMiddleware."""
    pytest.importorskip("deepagents")
    import deepagents

    from harness.backends import deepagents_backend as da
    from harness.types import ExecRequest

    captured: dict = {}
    monkeypatch.setattr(
        deepagents,
        "create_deep_agent",
        lambda *a, **kw: captured.setdefault("middleware", kw["middleware"]) and object(),
    )
    da._build_agent(ExecRequest(prompt="x", workspace=tmp_path, model="openai:qwen3.5-9b-mlx"))
    names = [type(m).__name__ for m in captured["middleware"]]
    salvage_idx = names.index("ToolSalvageMiddleware")
    empty_idx = names.index("EmptyTurnMiddleware")
    assert salvage_idx == empty_idx - 1
