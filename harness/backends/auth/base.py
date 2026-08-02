"""Slot de auth plugável: o Protocol e nada além de um adapter nulo.

Adapter de OAuth de assinatura em cliente de terceiro é ÁREA CINZENTA DE ToS e
NÃO faz parte deste repo (risco 7 da SPEC): só o slot entra: quem quiser um
publica o próprio pacote e se anuncia no entry point `harness.auth`.
"""

from __future__ import annotations

from typing import ClassVar, Mapping, Protocol, runtime_checkable

from harness.types import Preflight

__all__ = ["AuthAdapter", "NullAuth", "Preflight"]


@runtime_checkable
class AuthAdapter(Protocol):
    name: ClassVar[str]

    def env(self) -> Mapping[str, str]:
        """Variáveis injetadas no processo do backend. Nunca persistidas em disco."""
        ...

    def check(self) -> Preflight:
        """Checagem local da credencial. ZERO chamada de LLM."""
        ...


class NullAuth:
    """Default: o backend usa a autenticação nativa da própria ferramenta."""

    name: ClassVar[str] = "null"

    def env(self) -> Mapping[str, str]:
        return {}

    def check(self) -> Preflight:
        return Preflight(ok=True, reason="sem adapter — autenticação nativa do backend")
