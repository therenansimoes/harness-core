"""Salvage tool calls embedded as text in the model response content.

Hermes/Qwopus models sometimes emit tool markup in the *content* field instead
of as structured tool_calls (especially when the thinking block is long).  This
middleware intercepts those turns, parses the markup, and converts valid calls
into proper tool_calls so the agent graph can dispatch them normally.  The
nudge from EmptyTurnMiddleware (which runs after us) therefore never fires for
a real call that merely landed in the wrong field.

Supported formats
-----------------
1. Hermes XML tags::

       <tool_call>
       {"name": "write_file", "arguments": {"path": "x.py", "content": "…"}}
       </tool_call>

2. Bare JSON objects anywhere in the content::

       {"name": "write_file", "arguments": {"path": "x.py", "content": "…"}}

In both cases ``arguments`` (OpenAI style) and ``args`` (LangChain style) are
accepted.  Calls whose ``name`` does not match ``[A-Za-z0-9_-]+`` are silently
dropped (gating on valid names).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from copy import copy
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

_log = logging.getLogger(__name__)

# Match <tool_call>…</tool_call> (possibly with whitespace around the JSON).
_HERMES_TAG = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)

# A valid tool name: letters, digits, underscores, hyphens — no spaces.
_VALID_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _parse_calls(text: str) -> list[dict]:
    """Return a list of tool-call dicts parsed from *text*."""
    results: list[dict] = []

    # 1. Hermes tags — highest fidelity.
    for m in _HERMES_TAG.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        tc = _normalize(obj)
        if tc:
            results.append(tc)

    if results:
        return results

    # 2. Bare JSON objects anywhere in the text (fallback).
    # Walk the string looking for { … } objects at the top level.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start : i + 1]
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError:
                    pass
                else:
                    tc = _normalize(obj)
                    if tc:
                        results.append(tc)
                start = -1

    return results


def _normalize(obj: Any) -> dict | None:
    """Convert a parsed JSON object to a LangChain tool_call dict, or None."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or ""
    if not _VALID_NAME.match(str(name)):
        return None
    # Accept both "arguments" (OpenAI/Hermes) and "args" (LangChain).
    args = obj.get("arguments") or obj.get("args") or {}
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if isinstance(args, str) else {}
        except json.JSONDecodeError:
            args = {}
    return {
        "name": name,
        "args": args,
        "id": f"salvage_{uuid.uuid4().hex[:8]}",
        "type": "tool_call",
    }


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
            return msg
    return None


def _salvage(response: Any) -> Any:
    """Return a (possibly modified) response with tool calls rescued from content."""
    ai = _ai_from_response(response)
    if ai is None:
        return response

    # Already has structured tool_calls — nothing to do.
    if getattr(ai, "tool_calls", None):
        return response

    content = getattr(ai, "content", None) or ""
    if isinstance(content, list):
        # Flatten multimodal content to text for scanning.
        content = " ".join(
            (c.get("text") or "") if isinstance(c, dict) else str(c) for c in content
        )

    calls = _parse_calls(content)
    if not calls:
        return response

    _log.info("tool_salvage: rescued %d tool call(s) from content", len(calls))

    # Patch the AIMessage in-place on the response.
    try:
        patched = copy(ai)
        object.__setattr__(patched, "tool_calls", calls)
    except Exception:
        try:
            patched = AIMessage(content=content, tool_calls=calls)
        except Exception:
            return response

    result = getattr(response, "result", None)
    if result is not None:
        # Replace the last AI message in the result list.
        new_result = list(result)
        for i in range(len(new_result) - 1, -1, -1):
            msg = new_result[i]
            if isinstance(msg, AIMessage) or getattr(msg, "type", None) in ("ai", "assistant"):
                new_result[i] = patched
                break
        try:
            response.result = new_result
        except AttributeError:
            pass
    elif isinstance(response, AIMessage):
        response = patched

    return response


class ToolSalvageMiddleware(AgentMiddleware):
    """Rescue Hermes/bare-JSON tool calls that landed in content instead of tool_calls."""

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        resp = handler(request)
        return _salvage(resp)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        resp = await handler(request)
        return _salvage(resp)


__all__ = ["ToolSalvageMiddleware", "_parse_calls", "_normalize"]
