import pytest

from harness.backends import registry
from harness.backends.base import Backend
from harness.backends.mock import MockBackend


def test_mock_is_available():
    assert "mock" in registry.available()


def test_get_backend_returns_protocol_impl():
    backend = registry.get_backend("mock")
    assert isinstance(backend, MockBackend)
    assert isinstance(backend, Backend)
    assert backend.name == "mock"


def test_unknown_backend_raises():
    with pytest.raises(KeyError):
        registry.get_backend("nao-existe")


def test_manual_registration_wins():
    registry.register("mock-clone", MockBackend)
    try:
        assert "mock-clone" in registry.available()
        assert registry.get_backend("mock-clone").preflight().ok
    finally:
        registry.unregister("mock-clone")
    assert "mock-clone" not in registry.available()
