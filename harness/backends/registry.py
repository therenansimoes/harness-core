"""Descoberta de backends por entry point `harness.backends`, com registro
manual como fallback (plugin de terceiro não precisa tocar no núcleo).
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Callable

from harness.backends.base import Backend

ENTRY_POINT_GROUP = "harness.backends"

_manual: dict[str, Callable[[], Backend]] = {}


def register(name: str, factory: Callable[[], Backend]) -> None:
    """Registro manual — usado por plugins não instalados e por testes."""
    _manual[name] = factory


def unregister(name: str) -> None:
    _manual.pop(name, None)


def _entry_point_factories() -> dict[str, Callable[[], Backend]]:
    found: dict[str, Callable[[], Backend]] = {}
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover — API pré-3.10
        eps = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[assignment]
    for ep in eps:
        found[ep.name] = _ep_factory(ep)
    return found


def _ep_factory(ep) -> Callable[[], Backend]:
    def factory() -> Backend:
        return ep.load()()

    return factory


def _factories() -> dict[str, Callable[[], Backend]]:
    # Fallback embutido: os backends do repo existem mesmo sem o pacote
    # instalado (o `deepagents` se anuncia indisponível no preflight).
    factories: dict[str, Callable[[], Backend]] = {
        "mock": _builtin_mock,
        "deepagents": _builtin_deepagents,
    }
    factories.update(_entry_point_factories())
    factories.update(_manual)
    return factories


def _builtin_mock() -> Backend:
    from harness.backends.mock import MockBackend

    return MockBackend()


def _builtin_deepagents() -> Backend:
    from harness.backends.deepagents_backend import DeepagentsBackend

    return DeepagentsBackend()


def available() -> list[str]:
    return sorted(_factories())


def get_backend(name: str) -> Backend:
    factories = _factories()
    if name not in factories:
        raise KeyError(f"backend desconhecido: {name!r} (disponíveis: {', '.join(sorted(factories))})")
    return factories[name]()
