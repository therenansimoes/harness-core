"""Precedência de `harness.paths`: env > árvore do cwd > `~/.harness`.

O que estes testes protegem é a INVARIANTE do checkout: rodando da raiz do
repo, `config_dir()`/`data_dir()` continuam sendo `config/` e `data/`
relativos, que é o que a suíte inteira assume.
"""

from pathlib import Path

import pytest

from harness import paths


@pytest.fixture
def clean_env(monkeypatch):
    """Ambiente sem nenhuma das envs de path — o default é o que está sob teste."""
    for env in (paths.HOME_ENV, paths.CONFIG_DIR_ENV, paths.DATA_DIR_ENV):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def fake_home(tmp_path, monkeypatch, clean_env):
    """`HOME_ROOT` num tmpdir, e cwd num diretório SEM `config/`."""
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "HOME_ROOT", home)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    return home


def _checkout(tmp_path, monkeypatch):
    """cwd com `config/` — o que faz a resolução legada valer."""
    root = tmp_path / "checkout"
    (root / "config").mkdir(parents=True)
    monkeypatch.chdir(root)
    return root


# --- precedência ----------------------------------------------------------------


def test_env_vence_cwd_e_home(tmp_path, monkeypatch, clean_env):
    _checkout(tmp_path, monkeypatch)
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "dat"))

    assert paths.config_dir() == tmp_path / "cfg"
    assert paths.data_dir() == tmp_path / "dat"


def test_cwd_com_config_mantem_resolucao_legada(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(paths, "HOME_ROOT", tmp_path / "home")
    _checkout(tmp_path, monkeypatch)

    # Relativos de propósito: é o comportamento histórico do router e do ledger.
    assert paths.config_dir() == Path("config")
    assert paths.data_dir() == Path("data")


def test_cwd_limpo_cai_no_home(fake_home):
    assert paths.config_dir() == fake_home / "config"
    assert paths.data_dir() == fake_home / "data"


def test_home_env_vence_home_root_do_import(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(paths, "HOME_ROOT", tmp_path / "importado")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "da-env"))

    assert paths.home_root() == tmp_path / "da-env"
    assert paths.config_dir() == tmp_path / "da-env" / "config"


# --- defaults empacotados -------------------------------------------------------


def test_source_root_e_packaged_defaults_no_checkout():
    root = paths.source_root()
    assert root is not None and (root / "config" / "ruler.toml").is_file()
    # Sem `harness/_defaults/` (só existe na wheel), o checkout É o default.
    assert paths.packaged_defaults() == root


def test_config_file_cai_no_packaged_quando_ausente(fake_home):
    resolved = paths.config_file("ruler.toml")

    assert resolved == paths.packaged_defaults() / "config" / "ruler.toml"
    assert resolved.is_file()


def test_config_file_prefere_o_config_dir(tmp_path, monkeypatch, clean_env):
    local = tmp_path / "cfg"
    local.mkdir()
    (local / "ruler.toml").write_text("[gate]\n", encoding="utf-8")
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(local))

    assert paths.config_file("ruler.toml") == local / "ruler.toml"


def test_skills_dir_segue_o_config_dir(tmp_path, monkeypatch, clean_env):
    tree = tmp_path / "tree"
    (tree / "config").mkdir(parents=True)
    (tree / "skills").mkdir()
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tree / "config"))

    assert paths.skills_dir() == tree / "skills"


def test_skills_dir_cai_no_packaged_sem_irmao(fake_home):
    assert paths.skills_dir() == paths.packaged_defaults() / "skills"


# --- ensure_user_config ---------------------------------------------------------


def test_ensure_user_config_semeia_e_e_idempotente(fake_home):
    first = paths.ensure_user_config()
    seeded = sorted(p.name for p in first.glob("*.toml"))

    assert first == fake_home / "config"
    assert "ruler.toml" in seeded
    assert (fake_home / "data").is_dir()

    # Segunda passada não muda nada e não levanta.
    assert paths.ensure_user_config() == first
    assert sorted(p.name for p in first.glob("*.toml")) == seeded


def test_ensure_user_config_nunca_sobrescreve(fake_home):
    conf = paths.ensure_user_config()
    editado = conf / "ruler.toml"
    editado.write_text("[gate]\nkpi_regression_tolerance = 0.5\n", encoding="utf-8")

    paths.ensure_user_config()

    assert "0.5" in editado.read_text(encoding="utf-8")


def test_ensure_user_config_traz_arquivo_novo_sem_tocar_no_velho(fake_home):
    conf = paths.ensure_user_config()
    editado = conf / "ruler.toml"
    editado.write_text("# meu\n", encoding="utf-8")
    (conf / "kinds.toml").unlink()

    paths.ensure_user_config()

    assert (conf / "kinds.toml").is_file()  # o que faltava voltou
    assert editado.read_text(encoding="utf-8") == "# meu\n"  # o que existia ficou
