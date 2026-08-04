"""Redação: cada padrão de segredo sai do texto que vira evidência."""

import pytest

from harness.redact import MASK, env_secrets, redact


@pytest.mark.parametrize(
    "texto,segredo",
    [
        ("OPENAI_API_KEY=sk-abc123DEF456ghi789", "sk-abc123DEF456ghi789"),
        ("token: sk-ant-api03-AAAbbbCCCdddEEEfff", "sk-ant-api03-AAAbbbCCCdddEEEfff"),
        ("clonando com ghp_0123456789abcdefghij", "ghp_0123456789abcdefghij"),
        ("gho_ABCDEFGHIJKLMNOPQRST no remote", "gho_ABCDEFGHIJKLMNOPQRST"),
        ("aws id AKIAIOSFODNN7EXAMPLE ok", "AKIAIOSFODNN7EXAMPLE"),
        ("slack xoxb-123456789012-abcdef", "xoxb-123456789012-abcdef"),
        ("key=AIzaSyA1234567890abcdefghijklmnopqrstuv", "AIzaSyA1234567890abcdefghijklmnopqrstuv"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ("senha=hunter2supersecreta", "hunter2supersecreta"),
        ("DB_PASSWORD: 'p4ssw0rd-longa'", "p4ssw0rd-longa"),
        ("MY_SECRET_TOKEN=deadbeefcafe", "deadbeefcafe"),
    ],
)
def test_padrao_sai(texto, segredo):
    out = redact(texto)
    assert segredo not in out
    assert MASK in out


def test_esquema_do_bearer_fica():
    # Diagnóstico depende de saber QUE havia Authorization; o token é que não.
    assert redact("Authorization: Bearer abcdef1234567890") == (
        f"Authorization: Bearer {MASK}"
    )


def test_texto_sem_segredo_intacto():
    log = "2 passed, 1 failed em test_foo.py::test_bar (rc=1)\nPATH=/usr/bin:/bin"
    assert redact(log) == log


def test_valor_de_env_de_nome_suspeito(monkeypatch):
    monkeypatch.setenv("ACME_API_KEY", "valor-livre-sem-forma-conhecida")
    monkeypatch.setenv("HARNESS_DATA_DIR", "/tmp/dados-nao-segredo")
    out = redact("conectando com valor-livre-sem-forma-conhecida em /tmp/dados-nao-segredo")
    assert "valor-livre-sem-forma-conhecida" not in out
    # Nome que não casa KEY|TOKEN|SECRET|PASSWORD não é mascarado.
    assert "/tmp/dados-nao-segredo" in out


def test_env_curto_nao_conta(monkeypatch):
    # `TOKEN=a` mascararia todo "a" do log inteiro.
    monkeypatch.setenv("X_TOKEN", "abc")
    assert redact("abc def") == "abc def"


def test_env_secrets_maior_primeiro():
    vals = env_secrets({"A_TOKEN": "segredo-curto", "B_KEY": "segredo-curto-mais-longo"})
    assert vals == ["segredo-curto-mais-longo", "segredo-curto"]


def test_idempotente():
    uma = redact("api_key=sk-abcdefghijklmnop")
    assert redact(uma) == uma


def test_multilinha_pega_todas():
    out = redact("linha1 ghp_0123456789abcdefghij\nlinha2 AKIAIOSFODNN7EXAMPLE\nlinha3 ok")
    assert "ghp_" not in out and "AKIA" not in out
    assert "linha3 ok" in out
