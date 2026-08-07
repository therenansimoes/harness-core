"""Tests for CompletionGuardMiddleware."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from harness.backends.completion_guard import (
    CompletionGuardMiddleware,
    _files_from_verify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(tool_calls=None, content="done"):
    """Fake agent response with optional tool_calls."""
    ai = AIMessage(content=content, tool_calls=tool_calls or [])
    resp = MagicMock()
    resp.result = [ai]
    return resp


def _stop_response():
    """Response with no tool_calls (model stopped)."""
    return _make_response(tool_calls=[], content="")


def _request_with_messages(msgs=None):
    req = MagicMock()
    req.messages = list(msgs or [HumanMessage(content="do the task")])
    return req


# ---------------------------------------------------------------------------
# _files_from_verify
# ---------------------------------------------------------------------------


def test_files_from_verify_test_f():
    cmd = "test -f output.py && test -f helper.py"
    result = _files_from_verify(cmd)
    assert "output.py" in result
    assert "helper.py" in result


def test_files_from_verify_empty():
    assert _files_from_verify("") == ()


def test_files_from_verify_dedup():
    cmd = "test -f foo.py && test -f foo.py"
    assert _files_from_verify(cmd).count("foo.py") == 1


# ---------------------------------------------------------------------------
# CompletionGuardMiddleware — sync
# ---------------------------------------------------------------------------


def test_missing_file_nudges_once(tmp_path: Path):
    """Stop + missing file → nudge injected, then allow second stop through."""
    expected = ("missing.py",)
    guard = CompletionGuardMiddleware(expected_files=expected, workspace=tmp_path)

    stop_resp = _stop_response()
    nudged_stop = _stop_response()
    handler = MagicMock(side_effect=[stop_resp, nudged_stop])
    req = _request_with_messages()

    result = guard.wrap_model_call(req, handler)

    assert handler.call_count == 2
    # Second call should have a HumanMessage mentioning the missing file
    second_req = handler.call_args_list[1][0][0]
    nudge_msg = second_req.messages[-1]
    assert isinstance(nudge_msg, HumanMessage)
    assert "missing.py" in nudge_msg.content
    assert result is nudged_stop


def test_all_present_no_nudge(tmp_path: Path):
    """All expected files exist → pass through without nudge."""
    f = tmp_path / "output.py"
    f.write_text("x = 1")
    guard = CompletionGuardMiddleware(expected_files=("output.py",), workspace=tmp_path)

    stop_resp = _stop_response()
    handler = MagicMock(return_value=stop_resp)
    req = _request_with_messages()

    result = guard.wrap_model_call(req, handler)

    assert handler.call_count == 1
    assert result is stop_resp


def test_empty_expected_no_op(tmp_path: Path):
    """No expected files → middleware is a no-op."""
    guard = CompletionGuardMiddleware(expected_files=(), workspace=tmp_path)

    stop_resp = _stop_response()
    handler = MagicMock(return_value=stop_resp)
    req = _request_with_messages()

    result = guard.wrap_model_call(req, handler)

    assert handler.call_count == 1
    assert result is stop_resp


def test_second_stop_after_nudge_allowed(tmp_path: Path):
    """After the nudge fires once, a subsequent stop goes through unconditionally."""
    expected = ("still_missing.py",)
    guard = CompletionGuardMiddleware(expected_files=expected, workspace=tmp_path)

    stop1 = _stop_response()
    stop2 = _stop_response()
    stop3 = _stop_response()
    handler = MagicMock(side_effect=[stop1, stop2])
    req = _request_with_messages()

    # First wrap_model_call: stop + missing → nudge (calls handler twice)
    guard.wrap_model_call(req, handler)
    assert guard._nudged is True

    # Second wrap_model_call: nudged=True → no more nudges
    handler2 = MagicMock(return_value=stop3)
    result = guard.wrap_model_call(req, handler2)
    assert handler2.call_count == 1
    assert result is stop3


def test_tool_calls_not_nudged(tmp_path: Path):
    """Response with tool_calls → no nudge even if files are missing."""
    expected = ("missing.py",)
    guard = CompletionGuardMiddleware(expected_files=expected, workspace=tmp_path)

    with_tools = _make_response(
        tool_calls=[{"name": "write_file", "args": {}, "id": "1", "type": "tool_call"}],
        content="",
    )
    handler = MagicMock(return_value=with_tools)
    req = _request_with_messages()

    result = guard.wrap_model_call(req, handler)

    assert handler.call_count == 1
    assert result is with_tools


# ---------------------------------------------------------------------------
# CompletionGuardMiddleware — async
# ---------------------------------------------------------------------------


def test_async_missing_file_nudges_once(tmp_path: Path):
    """Async path: stop + missing file → nudge once."""
    expected = ("async_missing.py",)
    guard = CompletionGuardMiddleware(expected_files=expected, workspace=tmp_path)

    stop_resp = _stop_response()
    nudged_resp = _stop_response()
    calls: list = []

    async def handler(req):
        calls.append(req)
        return stop_resp if len(calls) == 1 else nudged_resp

    req = _request_with_messages()
    result = asyncio.run(guard.awrap_model_call(req, handler))

    assert len(calls) == 2
    nudge_msg = calls[1].messages[-1]
    assert isinstance(nudge_msg, HumanMessage)
    assert "async_missing.py" in nudge_msg.content
    assert result is nudged_resp


def test_async_all_present_no_nudge(tmp_path: Path):
    f = tmp_path / "async_out.py"
    f.write_text("x = 1")
    guard = CompletionGuardMiddleware(expected_files=("async_out.py",), workspace=tmp_path)

    stop_resp = _stop_response()
    calls: list = []

    async def handler(req):
        calls.append(req)
        return stop_resp

    req = _request_with_messages()
    result = asyncio.run(guard.awrap_model_call(req, handler))
    assert len(calls) == 1
    assert result is stop_resp
