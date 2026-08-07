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
import os
from collections.abc import Iterator
from typing import Any

# Upstream model id (LM Studio or mlx_lm). Set to the id from GET /v1/models
# (e.g. `bonsai` after `lms load … --identifier bonsai`).
UPSTREAM_MODEL_ENV = "HARNESS_MLX_SERVED_MODEL"  # legacy name; works for LMS too
UPSTREAM_MODEL_ENV_ALT = "HARNESS_UPSTREAM_MODEL"

# Cursor Agent dumps huge contexts (~200k tokens). Cap chars before upstream
# or the Mac pages to death. Override via env.
PROMPT_CHARS_ENV = "HARNESS_SERVE_MAX_PROMPT_CHARS"
DEFAULT_MAX_PROMPT_CHARS = 24_000  # ~6k tokens; keep TTFT sane under Cursor dumps
MAX_SINGLE_MSG_CHARS = 8_000
MEM_FREE_WARN_MB_ENV = "HARNESS_SERVE_MEM_WARN_MB"
DEFAULT_MEM_WARN_MB = 2_048  # warn/log when free RAM below this
MAX_TOKENS_ENV = "HARNESS_SERVE_MAX_TOKENS"
DEFAULT_MAX_TOKENS = 4_096
DISABLE_THINKING_ENV = "HARNESS_SERVE_DISABLE_THINKING"  # "0" to keep model CoT

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

# Cursor Agent local passthrough — LMS id (tools + thinking go through as-is).
CURSOR_LOCAL_ID = "qwopus3.5-4b-coder-mtp"
BONSAI_ID = CURSOR_LOCAL_ID  # legacy export name used by serve.py / tests
# Stable aliases Cursor / docs may send. Comparison is casefold + strip openai:.
CURSOR_LOCAL_ALIASES = frozenset(
    {
        "qwopus3.5-4b-coder-mtp",
        "qwopus",
        "qwopus3.5-4b-coder",
        # legacy Cursor configs still pointing at bonsai
        "bonsai",
        "prism-ml/bonsai-27b",
        "prism-ml/bonsai-27b-mlx-1bit",
        "bonsai-27b",
    }
)
BONSAI_ALIASES = CURSOR_LOCAL_ALIASES  # legacy alias


def normalize_model_id(raw: str | None) -> str:
    if not isinstance(raw, str):
        return ""
    key = raw.strip()
    if key.lower().startswith("openai:"):
        key = key[7:]
    return key.strip()


def is_cursor_local_model(raw: str | None) -> bool:
    """True when the client asked for the local LMS passthrough (qwopus)."""
    key = normalize_model_id(raw).casefold()
    if not key:
        return False
    aliases = {a.casefold() for a in CURSOR_LOCAL_ALIASES}
    return key in aliases or key.endswith("/bonsai-27b") or key.endswith("qwopus3.5-4b-coder-mtp")


def is_bonsai_model(raw: str | None) -> bool:
    """Legacy name — same as is_cursor_local_model."""
    return is_cursor_local_model(raw)


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


def _part_text(part: Any) -> str:
    """Extract plain text from one OpenAI/Responses content part; drop images."""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    typ = part.get("type")
    if typ in ("text", "input_text", "output_text"):
        return str(part.get("text") or "")
    if typ in ("image_url", "input_image", "image", "input_file", "file"):
        return ""  # mlx_lm text-only — Cursor often attaches screenshots
    # Unknown dict part: prefer .text if present
    if isinstance(part.get("text"), str):
        return part["text"]
    return ""


def flatten_message_content(content: Any) -> str:
    """mlx_lm rejects non-text content types — always coerce to a string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_part_text(p) for p in content)
    return str(content)


def _flatten_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = dict(msg)
        if "content" in m:
            m["content"] = flatten_message_content(m.get("content"))
        out.append(m)
    return out



def _max_prompt_chars() -> int:
    raw = (os.environ.get(PROMPT_CHARS_ENV) or "").strip()
    if raw:
        try:
            return max(4_000, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_PROMPT_CHARS


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        total += len(c) if isinstance(c, str) else len(str(c or ""))
    return total


def _clip_str(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head - 16)
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]


def trim_messages(
    messages: list[dict[str, Any]],
    *,
    max_chars: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop oldest turns / clip huge messages so local inference stays alive.

    Keeps at most one system message and the newest turns that fit the budget,
    always preferring the latest user turn.
    """
    budget = _max_prompt_chars() if max_chars is None else max_chars
    stats: dict[str, Any] = {
        "before_chars": _messages_chars(messages),
        "before_msgs": len(messages),
        "budget": budget,
        "trimmed": False,
    }
    if not messages:
        stats["after_chars"] = 0
        stats["after_msgs"] = 0
        return [], stats

    capped: list[dict[str, Any]] = []
    for m in messages:
        mm = dict(m)
        c = mm.get("content")
        if isinstance(c, str) and len(c) > MAX_SINGLE_MSG_CHARS:
            mm["content"] = _clip_str(c, MAX_SINGLE_MSG_CHARS)
            stats["trimmed"] = True
        capped.append(mm)

    system = next((dict(m) for m in capped if m.get("role") == "system"), None)
    rest = [dict(m) for m in capped if m.get("role") != "system"]

    # Build from newest → oldest until budget filled
    chosen_rev: list[dict[str, Any]] = []
    sys_list = []
    if system is not None:
        # reserve up to 25% of budget for system
        sys_budget = max(2_000, budget // 4)
        c = system.get("content")
        if isinstance(c, str) and len(c) > sys_budget:
            system["content"] = _clip_str(c, sys_budget)
            stats["trimmed"] = True
        sys_list = [system]

    for m in reversed(rest):
        trial = sys_list + list(reversed(chosen_rev + [m]))
        if chosen_rev and _messages_chars(trial) > budget:
            stats["trimmed"] = True
            break
        chosen_rev.append(m)

    final = sys_list + list(reversed(chosen_rev))
    # Last message must fit remaining room
    if final:
        head_chars = _messages_chars(final[:-1])
        room = max(2_000, budget - head_chars)
        last = dict(final[-1])
        c = last.get("content")
        if isinstance(c, str) and len(c) > room:
            last["content"] = _clip_str(c, room)
            final[-1] = last
            stats["trimmed"] = True

    if len(final) != len(capped):
        stats["trimmed"] = True
    stats["after_chars"] = _messages_chars(final)
    stats["after_msgs"] = len(final)
    return final, stats


def memory_snapshot() -> dict[str, Any]:
    """Best-effort macOS free/inactive memory in MB — never raises."""
    out: dict[str, Any] = {"ok": False}
    try:
        import subprocess

        raw = subprocess.check_output(["vm_stat"], text=True, timeout=2.0)
        page = 4096
        for line in raw.splitlines():
            if "page size of" in line:
                for tok in line.replace("(", " ").split():
                    if tok.isdigit():
                        page = int(tok)
                        break
        counts: dict[str, int] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            digits = "".join(ch for ch in v if ch.isdigit())
            if digits:
                counts[k.strip()] = int(digits)
        free = counts.get("Pages free", 0) + counts.get("Pages speculative", 0)
        inactive = counts.get("Pages inactive", 0)
        out.update(
            {
                "ok": True,
                "free_mb": (free * page) // (1024 * 1024),
                "inactive_mb": (inactive * page) // (1024 * 1024),
                "available_mb": ((free + inactive) * page) // (1024 * 1024),
                "page_bytes": page,
            }
        )
    except Exception as exc:
        out["error"] = str(exc)
    return out


def memory_pressure_high() -> tuple[bool, dict[str, Any]]:
    snap = memory_snapshot()
    warn = DEFAULT_MEM_WARN_MB
    raw = (os.environ.get(MEM_FREE_WARN_MB_ENV) or "").strip()
    if raw:
        try:
            warn = max(256, int(raw))
        except ValueError:
            pass
    snap["warn_mb"] = warn
    if not snap.get("ok"):
        return False, snap
    return int(snap.get("available_mb") or 0) < warn, snap



def _max_tokens_cap() -> int:
    raw = (os.environ.get(MAX_TOKENS_ENV) or "").strip()
    if raw:
        try:
            return max(64, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_TOKENS


def _thinking_disabled() -> bool:
    # Default OFF → thinking ON. Set HARNESS_SERVE_DISABLE_THINKING=1 to disable CoT.
    return (os.environ.get(DISABLE_THINKING_ENV) or "0").strip() == "1"


def _clamp_max_tokens(out: dict[str, Any]) -> None:
    """Cursor often asks for huge max_tokens; local decode stays short."""
    cap = _max_tokens_cap()
    raw = out.get("max_tokens")
    try:
        cur = int(raw) if raw is not None else cap
    except (TypeError, ValueError):
        cur = cap
    out["max_tokens"] = max(1, min(cur, cap))


def normalize_chat_body(body: dict[str, Any]) -> dict[str, Any]:
    """Return a Chat Completions body safe to POST to the local upstream."""
    if not isinstance(body, dict):
        return {}
    out = dict(body)

    if _is_responses_body(out):
        out["messages"] = _input_to_messages(out.get("input"))

    if isinstance(out.get("messages"), list):
        out["messages"] = _flatten_messages(out["messages"])
        out["messages"], out["_harness_trim"] = trim_messages(out["messages"])

    if "max_output_tokens" in out and "max_tokens" not in out:
        try:
            out["max_tokens"] = int(out["max_output_tokens"])
        except (TypeError, ValueError):
            pass
    _clamp_max_tokens(out)

    kwargs = out.get("chat_template_kwargs")
    if not isinstance(kwargs, dict):
        kwargs = {}
    else:
        kwargs = dict(kwargs)
    kwargs["enable_thinking"] = not _thinking_disabled()
    out["chat_template_kwargs"] = kwargs

    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [_normalize_tool(t) for t in tools]

    # Client sees `bonsai`; upstream needs the id mlx_lm actually serves
    # (local path or HF id from GET /v1/models). Override via env.
    model = normalize_model_id(out.get("model") if isinstance(out.get("model"), str) else None)
    if is_cursor_local_model(model):
        upstream = (
            (os.environ.get(UPSTREAM_MODEL_ENV_ALT) or "").strip()
            or (os.environ.get(UPSTREAM_MODEL_ENV) or "").strip()
        )
        # Always pin to the LMS id unless HARNESS_UPSTREAM_MODEL overrides.
        out["model"] = upstream or CURSOR_LOCAL_ID

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
