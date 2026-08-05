"""SafeShellBackend + sandbox de SO.

A cerca (denylist) roda PRIMEIRO sobre o comando ORIGINAL — o comando
embrulhado contém paths absolutos (/usr/bin/sandbox-exec, profile no temp)
que a própria cerca recusaria. Depois de embrulhado, negação do Seatbelt
volta como output normal da tool (rc != 0 + "Operation not permitted"):
o modelo vê e corrige.
"""

from __future__ import annotations

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from harness.backends.safe_shell import (
    _HINT,
    _PREFIX,
    BLOCKED_EXIT_CODE,
    MAX_TIMEOUT,
    SafeShellBackend,
    _output_explicito,
)
from harness.backends.sandbox import SandboxStrategy


class SandboxedShellBackend(SafeShellBackend):
    """SafeShellBackend cujo subprocess roda embrulhado no sandbox de SO."""

    def __init__(self, *args, sandbox: SandboxStrategy, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sandbox = sandbox

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        # Espelha SafeShellBackend.execute trocando só o comando que vai ao
        # subprocess; a duplicação de ~8 linhas é intencional para manter
        # safe_shell.py intacto.
        reason = self._blocked_reason(command)
        if reason:
            return ExecuteResponse(
                output=f"{_PREFIX}: {reason}; {_HINT}",
                exit_code=BLOCKED_EXIT_CODE,
                truncated=False,
            )
        if timeout is not None:
            timeout = max(1, min(timeout, MAX_TIMEOUT))
        return _output_explicito(
            LocalShellBackend.execute(self, self._sandbox.wrap(command), timeout=timeout)
        )


__all__ = ["SandboxedShellBackend"]
