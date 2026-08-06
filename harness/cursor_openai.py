"""Cursor BYOK ↔ OpenAI Chat Completions ↔ mlx_lm.server.

Cursor Agent often POSTs a Responses-API-shaped body to `/v1/chat/completions`
(`input` instead of `messages`, flat tools). mlx_lm speaks Chat Completions and
emits `reasoning` / `delta.reasoning`; Cursor's thoughts panel wants
`reasoning_content` as a string, finished before `content` deltas.

This module is pure: normalize request bodies and remap response JSON / SSE
lines. No HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

# Fields Cursor (or mixed clients) stuff into chat/completions that mlx_lm
# does not want — strip on the way in.
_STRIP_REQUEST_KEYS = frozenset(
    {
        "store",
        "include",
        "truncation",
        "prompt_cache_retention",
        "stream_options",
        "reasoning",
        "max_output_tokens",  # Responses name; we map to max_tokens when useful
        "input",  # converted → messages
    }
)

BONSAI_ID = "bonsai"
# Stable aliases Cursor / docs may send. Comparison is casefold + strip openai:.
BONSAI_ALIASES = frozenset(
    {
        "bonsai",
        "prism-ml/bonsai-27b",
        "prism-ml/bonsai-27b-mlx-1bit",
        "bonsai-27b",
    }
)


def normalize_model_id(raw: str | None) -> str:
    if not isinstance(raw, str):
        return ""
    key = raw.strip()
    if key.lower().startswith("openai:"):
        key = key[7:]
    return key.strip()


def is_bonsai_model(raw: str | None) -> bool:
    """True when the client asked for the local bonsai passthrough model."""
    key = normalize_model_id(raw).casefold()
    if not key:
        return False
    return key in {a.casefold() for a in BONSAI_ALIASES} or key.endswith("/bonsai-27b")


def _is_responses_body(body: dict[str, Any]) -> bool:
    return "input" in body and "messages" not in body


def _input_to_messages(inp: Any) -> list[dict[str, Any]]:
    """Best-effort Responses `input` → Chat Completions `messages`."""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    if not isinstance(inp, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in inp:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if isinstance(role, str) and ("content" in item or "parts" in item):
            msg = {"role": role, "content": item.get("content", item.get("parts", ""))}
            if "tool_calls" in item:
                msg["tool_calls"] = item["tool_calls"]
            if "tool_call_id" in item:
                msg["tool_call_id"] = item["tool_call_id"]
            messages.append(msg)
            continue
        # Responses item types: message / function_call_output / etc.
        typ = item.get("type")
        if typ == "message" or (typ is None and "content" in item):
            role = item.get("role") or "user"
            content = item.get("content")
            if isinstance(content, list):
                texts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("input_text", "text", "output_text"):
                        texts.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        texts.append(part)
                content = "".join(texts)
            messages.append({"role": role, "content": content if content is not None else ""})
        elif typ in ("function_call_output", "tool_result"):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("tool_call_id") or "",
                    "content": item.get("output") or item.get("content") or "",
                }
            )
    return messages


def _normalize_tool(tool: Any) -> Any:
    """Flat Responses tool → nested Chat Completions function tool.

    Custom tools (ApplyPatch grammar) pass through unchanged — Cursor expects
    them echoed in tool_calls.
    """
    if not isinstance(tool, dict):
        return tool
    typ = tool.get("type")
    if typ == "function" and "function" in tool:
        return tool  # already nested
    if typ == "function" and "name" in tool:
        params = tool.get("parameters") or tool.get("input_schema") or {}
        out = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": params if isinstance(params, dict) else {},
            },
        }
        return out
    if typ == "custom":
        return tool
    # Bare name without type — treat as function
    if "name" in tool and "function" not in tool and typ is None:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {},
            },
        }
    return tool


def normalize_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return a Chat Completions body safe to POST to mlx_lm.server."""
    if not isinstance(body, dict):
        return {}
    out = dict(body)

    if _is_responses_body(out):
        out["messages"] = _input_to_messages(out.get("input"))

    if "max_output_tokens" in out and "max_tokens" not in out:
        try:
            out["max_tokens"] = int(out["max_output_tokens"])
        except (TypeError, ValueError):
            pass

    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [_normalize_tool(t) for t in tools]

    # Force client-facing model id downstream callers may overwrite; upstream
    # mlx often ignores model and serves the CLI weight. Prefer `default` if
    # the alias is bonsai — many mlx builds only list `default`.
    model = normalize_model_id(out.get("model") if isinstance(out.get("model"), str) else None)
    if is_bonsai_model(model) or model in BONSAI_ALIASES or model.casefold() == BONSAI_ID:
        out["model"] = "default"

    for key in _STRIP_REQUEST_KEYS:
        out.pop(key, None)

    return out


def _reasoning_to_content_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Prefer plain text fields; never pass encrypted blobs as the string
        for k in ("content", "text", "summary"):
            v = value.get(k)
            if isinstance(v, str):
                return v
        return None
    return str(value)


def remap_message_reasoning(message: dict[str, Any]) -> dict[str, Any]:
    """In-place-ish copy: `reasoning` → `reasoning_content` (string)."""
    if not isinstance(message, dict):
        return message
    out = dict(message)
    if "reasoning_content" not in out or out.get("reasoning_content") in (None, ""):
        raw = out.pop("reasoning", None)
        mapped = _reasoning_to_content_string(raw)
        if mapped is not None:
            out["reasoning_content"] = mapped
    else:
        out.pop("reasoning", None)
    return out


def remap_completion_response(data: dict[str, Any], *, echo_model: str = BONSAI_ID) -> dict[str, Any]:
    """Non-streaming chat.completion JSON."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    out["model"] = echo_model
    choices = out.get("choices")
    if isinstance(choices, list):
        new_choices = []
        for ch in choices:
            if not isinstance(ch, dict):
                new_choices.append(ch)
                continue
            c = dict(ch)
            msg = c.get("message")
            if isinstance(msg, dict):
                c["message"] = remap_message_reasoning(msg)
            new_choices.append(c)
        out["choices"] = new_choices
    return out


def remap_sse_data_payload(payload: dict[str, Any], *, echo_model: str = BONSAI_ID) -> dict[str, Any]:
    """One parsed `data: {...}` chat.completion.chunk object."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    out["model"] = echo_model
    choices = out.get("choices")
    if not isinstance(choices, list):
        return out
    new_choices = []
    for ch in choices:
        if not isinstance(ch, dict):
            new_choices.append(ch)
            continue
        c = dict(ch)
        delta = c.get("delta")
        if isinstance(delta, dict):
            d = dict(delta)
            if "reasoning" in d and not d.get("reasoning_content"):
                mapped = _reasoning_to_content_string(d.pop("reasoning"))
                if mapped is not None:
                    d["reasoning_content"] = mapped
            else:
                d.pop("reasoning", None)
            c["delta"] = d
        new_choices.append(c)
    out["choices"] = new_choices
    return out


def iter_remapped_sse_lines(
    lines: Iterator[bytes] | Iterator[str],
    *,
    echo_model: str = BONSAI_ID,
) -> Iterator[bytes]:
    """Yield SSE frames (`data: …\\n\\n`) with reasoning remapped.

    Passes through `[DONE]` and non-JSON data lines unchanged (still framed).
    """
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        # Accept either full frames or single data lines
        stripped = line.strip("\r\n")
        if not stripped:
            continue
        data_part = stripped[5:].lstrip() if stripped.startswith("data:") else stripped
        if data_part == "[DONE]":
            yield b"data: [DONE]\n\n"
            continue
        try:
            payload = json.loads(data_part)
        except json.JSONDecodeError:
            yield f"data: {data_part}\n\n".encode()
            continue
        if isinstance(payload, dict):
            payload = remap_sse_data_payload(payload, echo_model=echo_model)
            yield b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"
        else:
            yield f"data: {data_part}\n\n".encode()
