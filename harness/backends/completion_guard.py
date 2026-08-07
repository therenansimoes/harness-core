"""Nudge the model once when it stops without writing expected output files.

When the model ends a turn with no tool_calls AND one or more paths listed in
`expected_files` are absent from the workspace, this middleware injects a single
HumanMessage listing the missing paths and retries. The second stop (whether or
not files are now present) is let through unconditionally — same one-shot
contract as EmptyTurnMiddleware.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

_log = logging.getLogger(__name__)

# Regex mirroring reflect._PATHISH — extracts file paths with extensions from
# a verify_cmd string when UnitSpec.files is empty.
_PATHISH = re.compile(r"[\w./*@-]*\w\.[A-Za-z]\w*")

NUDGE_TEMPLATE = (
    "[completion_guard] A tarefa ainda está incompleta. "
    "Os seguintes arquivos esperados não existem no workspace:\n"
    "{missing}\n"
    "Crie-os agora usando write_file ou edit_file."
)


def _files_from_verify(verify_cmd: str) -> tuple[str, ...]:
    """Extract bare file paths from a verify_cmd (test -f heuristic)."""
    return tuple(dict.fromkeys(_PATHISH.findall(verify_cmd)))


def _has_tool_calls(response: Any) -> bool:
    result = getattr(response, "result", None)
    if not result:
        if isinstance(response, AIMessage):
            return bool(getattr(response, "tool_calls", None))
        return False
    for msg in reversed(result):
        if isinstance(msg, AIMessage) or getattr(msg, "type", None) in ("ai", "assistant"):
            return bool(getattr(msg, "tool_calls", None))
    return False


def _nudge_request(request: Any, missing: list[str]) -> Any:
    body = NUDGE_TEMPLATE.format(missing="\n".join(f"  - {p}" for p in missing))
    msgs = [*list(request.messages), HumanMessage(content=body)]
    try:
        return replace(request, messages=msgs)
    except TypeError:
        request.messages = msgs
        return request


class CompletionGuardMiddleware(AgentMiddleware):
    """One nudge when the model stops before creating expected output files."""

    def __init__(
        self,
        expected_files: tuple[str, ...],
        workspace: Path,
    ) -> None:
        self._expected = expected_files
        self._workspace = workspace
        self._nudged = False

    def _missing(self) -> list[str]:
        if not self._expected:
            return []
        return [p for p in self._expected if not (self._workspace / p).exists()]

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        resp = handler(request)
        if _has_tool_calls(resp) or self._nudged:
            return resp
        missing = self._missing()
        if not missing:
            return resp
        _log.warning("completion_guard: stop with %d missing file(s) — nudging", len(missing))
        self._nudged = True
        try:
            return handler(_nudge_request(request, missing))
        except Exception:
            return resp

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        resp = await handler(request)
        if _has_tool_calls(resp) or self._nudged:
            return resp
        missing = self._missing()
        if not missing:
            return resp
        _log.warning("completion_guard: stop with %d missing file(s) — nudging", len(missing))
        self._nudged = True
        try:
            return await handler(_nudge_request(request, missing))
        except Exception:
            return resp


__all__ = ["NUDGE_TEMPLATE", "CompletionGuardMiddleware", "_files_from_verify"]
