"""Descoberta de adapters de auth por entry point `harness.auth`.

Espelha `harness.backends.registry`: o núcleo não conhece provedor nenhum, só o
slot. O único adapter embutido é `null` — cada backend autentica do jeito nativo
da ferramenta que ele dirige.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

from harness.backends.auth.base import AuthAdapter, NullAuth

ENTRY_POINT_GROUP = "harness.auth"
DEFAULT = "null"

__all__ = ["AuthAdapter", "NullAuth", "available", "get_auth", "register", "unregister"]

_manual: dict[str, Callable[[], AuthAdapter]] = {}


def register(name: str, factory: Callable[[], AuthAdapter]) -> None:
    """Registro manual — usado por plugins não instalados e por testes."""
    _manual[name] = factory


def unregister(name: str) -> None:
    _manual.pop(name, None)


def _entry_point_factories() -> dict[str, Callable[[], AuthAdapter]]:
    found: dict[str, Callable[[], AuthAdapter]] = {}
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — API pré-3.10
        eps = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[assignment]
    for ep in eps:
        found[ep.name] = _ep_factory(ep)
    return found


def _ep_factory(ep) -> Callable[[], AuthAdapter]:
    def factory() -> AuthAdapter:
        return ep.load()()

    return factory


def _factories() -> dict[str, Callable[[], AuthAdapter]]:
    factories: dict[str, Callable[[], AuthAdapter]] = {DEFAULT: NullAuth}
    factories.update(_entry_point_factories())
    factories.update(_manual)
    return factories


def available() -> list[str]:
    return sorted(_factories())


def get_auth(name: str = DEFAULT) -> AuthAdapter:
    factories = _factories()
    if name not in factories:
        nomes = ", ".join(sorted(factories))
        raise KeyError(f"auth desconhecido: {name!r} (disponíveis: {nomes})")
    return factories[name]()
