"""Contrato do backend-adapter: 3 métodos, nada mais.

Os dataclasses vivem em `harness.types` (fonte única) e são reexportados aqui
para que um adapter de terceiro importe só deste módulo.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

__all__ = ["Backend", "Capabilities", "ExecRequest", "ExecResult", "Preflight"]


@runtime_checkable
class Backend(Protocol):
    name: ClassVar[str]

    def capabilities(self) -> Capabilities: ...

    def preflight(self) -> Preflight:
        """Checagem local: binário existe, credencial presente. ZERO LLM."""
        ...

    def execute(self, req: ExecRequest) -> ExecResult: ...
