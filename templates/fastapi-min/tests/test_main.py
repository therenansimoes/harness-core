"""Testes da API. `TestClient` faz a requisição em processo — sem porta, sem
uvicorn, sem sleep. É por isso que `httpx` está nas deps de teste."""

from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_healthz_responde_ok():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_echo_valida_e_transforma():
    r = client.post("/echo", json={"message": "oi", "shout": True})
    assert r.status_code == 200
    assert r.json() == {"message": "OI", "length": 2}

    # Corpo inválido tem que morrer na borda, não na regra de negócio.
    assert client.post("/echo", json={"message": ""}).status_code == 422
