"""Fábrica do checkpointer — seleção por config, default intacto, fail-closed
em valor desconhecido."""

from pathlib import Path

import pytest

from harness.graph import checkpoint


def _write_graph_toml(tmp_path, monkeypatch, body: str) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "graph.toml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HARNESS_CONFIG_DIR", str(cfg))
    return cfg / "graph.toml"


def test_default_quando_arquivo_ausente(tmp_path):
    assert checkpoint.load_checkpointer_kind(tmp_path / "nope.toml") == "sqlite"


def test_default_quando_chave_ausente(tmp_path):
    p = tmp_path / "graph.toml"
    p.write_text("max_attempts = 2\n", encoding="utf-8")
    assert checkpoint.load_checkpointer_kind(p) == "sqlite"


def test_toml_torto_cai_no_default(tmp_path):
    # Mesma postura do load_policy para arquivo ilegível: fábrica.
    p = tmp_path / "graph.toml"
    p.write_text("checkpointer = [unclosed", encoding="utf-8")
    assert checkpoint.load_checkpointer_kind(p) == "sqlite"


def test_valor_desconhecido_fail_closed(tmp_path):
    p = tmp_path / "graph.toml"
    p.write_text('checkpointer = "postgres"\n', encoding="utf-8")
    with pytest.raises(checkpoint.CheckpointerConfigError, match="postgres"):
        checkpoint.load_checkpointer_kind(p)
    # Valor não-string presente também levanta — só chave AUSENTE cai no default.
    p2 = tmp_path / "graph2.toml"
    p2.write_text("checkpointer = 3\n", encoding="utf-8")
    with pytest.raises(checkpoint.CheckpointerConfigError):
        checkpoint.load_checkpointer_kind(p2)


def test_open_checkpointer_falha_na_entrada_nao_no_meio(tmp_path, monkeypatch):
    _write_graph_toml(tmp_path, monkeypatch, 'checkpointer = "banana"\n')
    with (
        pytest.raises(checkpoint.CheckpointerConfigError),
        checkpoint.open_checkpointer(tmp_path / "data"),
    ):
        pass
    # Nada foi aberto antes do erro de config.
    assert not (tmp_path / "data" / "checkpoints.sqlite").exists()


def test_fabrica_retorna_sqlite_por_config(tmp_path, monkeypatch):
    _write_graph_toml(tmp_path, monkeypatch, 'checkpointer = "sqlite"\n')
    with checkpoint.open_checkpointer(tmp_path / "data") as saver:
        assert type(saver).__name__ == "SqliteSaver"
        # Serde com allowlist preservada nos dois backends.
        assert type(saver.serde).__name__ == "JsonPlusSerializer"
    assert (tmp_path / "data" / "checkpoints.sqlite").is_file()


def test_fabrica_retorna_memory_por_config(tmp_path, monkeypatch):
    _write_graph_toml(tmp_path, monkeypatch, 'checkpointer = "memory"\n')
    with checkpoint.open_checkpointer(tmp_path / "data") as saver:
        assert type(saver).__name__ == "InMemorySaver"
        assert type(saver.serde).__name__ == "JsonPlusSerializer"
    # Backend memory não escreve arquivo de banco.
    assert not (tmp_path / "data" / "checkpoints.sqlite").exists()


def test_kind_explicito_ignora_config(tmp_path, monkeypatch):
    _write_graph_toml(tmp_path, monkeypatch, 'checkpointer = "sqlite"\n')
    with checkpoint.open_checkpointer(tmp_path / "data", kind="memory") as saver:
        assert type(saver).__name__ == "InMemorySaver"
    # kind explícito ainda é validado.
    with (
        pytest.raises(checkpoint.CheckpointerConfigError),
        checkpoint.open_checkpointer(tmp_path / "data", kind="redis"),
    ):
        pass
