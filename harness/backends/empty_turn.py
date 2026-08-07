"""Retry once when the model ends a turn with empty content and no tool_calls.

Local Qwopus/LMS often fills `reasoning_content` and returns blank `content`
with no tools (seen on micro_refactor_rename). The agent graph treats that as
"done" → stalled. One HumanMessage nudge + one retry recovers many of those
turns without spending the full max_turns budget on silence.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

_log = logging.getLogger(__name__)

NUDGE = (
    "[empty_turn] Last response had no content and no tool_call. "
    "Follow proc-recover-after-empty-turn NOW: call write_file on every "
    "expected output path to produce the file (small .py → write the whole "
    "file; do not read first, just write)."
)


def _ai_from_response(response: Any) -> AIMessage | None:
    result = getattr(response, "result", None)
    if not result:
        if isinstance(response, AIMessage):
            return response
        return None
    for msg in reversed(result):
        if isinstance(msg, AIMessage):
            return msg
        typ = getattr(msg, "type", None) or getattr(msg, "role", None)
        if typ in ("ai", "assistant"):
            return msg  # duck-typed
    return None


def _is_empty_stop(response: Any) -> bool:
    ai = _ai_from_response(response)
    if ai is None:
        return False
    content = getattr(ai, "content", None)
    if isinstance(content, list):
        # multimodal / blocks — treat as non-empty if any text/tool block
        text = "".join(
            (c.get("text") or "") if isinstance(c, dict) else str(c) for c in content
        ).strip()
        has_text = bool(text)
    else:
        has_text = bool((content or "").strip())
    tools = getattr(ai, "tool_calls", None) or []
    return (not has_text) and (not tools)


def _nudge_request(request: Any) -> Any:
    msgs = [*list(request.messages), HumanMessage(content=NUDGE)]
    try:
        return replace(request, messages=msgs)
    except TypeError:
        # Not a dataclass — mutate directly (duck-typed request objects)
        request.messages = msgs
        return request


class EmptyTurnMiddleware(AgentMiddleware):
    """One retry with a nudge when the model returns silence."""

    def wrap_model_call(self, request, handler):
        resp = handler(request)
        if not _is_empty_stop(resp):
            return resp
        _log.warning("empty_turn: no content and no tool_calls — nudging")
        try:
            return handler(_nudge_request(request))
        except Exception:
            return resp

    async def awrap_model_call(self, request, handler):
        resp = await handler(request)
        if not _is_empty_stop(resp):
            return resp
        _log.warning("empty_turn: no content and no tool_calls — nudging")
        try:
            return await handler(_nudge_request(request))
        except Exception:
            return resp


__all__ = ["NUDGE", "EmptyTurnMiddleware", "_is_empty_stop"]
