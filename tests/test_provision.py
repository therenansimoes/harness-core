import re
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import cli
from harness.workspace import provision as prov

CONFIG = Path(__file__).parents[1] / "config" / "tools.toml"


def _commit(root: Path, msg: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg],
        cwd=root,
        check=True,
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def repo(tmp_path):
    """Repo git com um cache (node_modules) fora do controle de versão."""
    if shutil.which("git") is None:
        pytest.skip("git não disponível")
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('oi')\n", encoding="utf-8")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "src"], cwd=root, check=True)
    _commit(root, "init")
    return root


def _worktree_list(repo: Path) -> str:
    return subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_provision_cria_worktree_e_symlink_de_cache(repo, data_dir):
    ws = prov.provision(repo, "run1", config_path=CONFIG)

    assert ws.path == data_dir / "ws" / "run1"
    assert ws.mode == "worktree"
    assert (ws.path / "src" / "app.py").is_file()

    link = ws.path / "node_modules"
    assert link.is_symlink()
    assert link.resolve() == (repo / "node_modules").resolve()
    assert (link / "left-pad" / "index.js").is_file()


def test_mesmo_run_id_reusa_worktree(repo, data_dir):
    a = prov.provision(repo, "run1", config_path=CONFIG)
    (a.path / "marca.txt").write_text("x", encoding="utf-8")

    b = prov.provision(repo, "run1", config_path=CONFIG)

    assert b.path == a.path
    assert (b.path / "marca.txt").is_file()  # reusou, não recriou
    assert (b.path / "node_modules").is_symlink()


def test_dispose_remove_worktree_e_limpa_registro(repo, data_dir):
    ws = prov.provision(repo, "run1", config_path=CONFIG)
    prov.dispose(ws, keep=False)

    assert not ws.path.exists()
    assert "run1" not in _worktree_list(repo)
    # symlink não arrasta o cache da origem junto
    assert (repo / "node_modules" / "left-pad" / "index.js").is_file()


def test_dispose_keep_preserva_workspace(repo, data_dir):
    ws = prov.provision(repo, "run1", config_path=CONFIG)
    prov.dispose(ws, keep=True)
    assert ws.path.is_dir()


def test_dispose_recusa_path_fora_do_data_dir(tmp_path, data_dir):
    fora = tmp_path / "fora"
    fora.mkdir()
    ws = prov.Workspace(path=fora, run_id="x", mode="tmpdir", repo=tmp_path)

    with pytest.raises(ValueError):
        prov.dispose(ws, keep=False)
    assert fora.is_dir()


def test_tmpdir_funciona_sem_git(tmp_path, data_dir):
    src = tmp_path / "plain"
    (src / "site").mkdir(parents=True)
    (src / "site" / "index.html").write_text("<h1>oi</h1>", encoding="utf-8")
    (src / ".cache").mkdir()
    (src / ".cache" / "big.bin").write_bytes(b"0" * 32)

    ws = prov.provision(src, "run-tmp", mode="tmpdir", config_path=CONFIG)

    assert (ws.path / "site" / "index.html").is_file()
    assert (ws.path / ".cache").is_symlink()  # cache não é copiado

    prov.dispose(ws, keep=False)
    assert not ws.path.exists()
    assert (src / ".cache" / "big.bin").is_file()


def test_worktree_em_repo_nao_git_falha_explicito(tmp_path, data_dir):
    src = tmp_path / "plain"
    src.mkdir()
    with pytest.raises(ValueError):
        prov.provision(src, "x", config_path=CONFIG)


def test_cache_versionado_nao_vira_symlink(repo, data_dir):
    (repo / ".cache").mkdir()
    (repo / ".cache" / "keep.txt").write_text("v", encoding="utf-8")
    subprocess.run(["git", "add", ".cache"], cwd=repo, check=True)
    _commit(repo, "versiona .cache")

    ws = prov.provision(repo, "run2", config_path=CONFIG)

    assert not (ws.path / ".cache").is_symlink()
    assert (ws.path / ".cache" / "keep.txt").is_file()


def test_data_dir_relativo_nao_nasce_dentro_do_repo(repo, tmp_path, monkeypatch):
    """Sem HARNESS_DATA_DIR, cwd LIMPO (sem `config/`) escreve em `$HARNESS_HOME`.

    O `data/` relativo só vale dentro de uma árvore com `config/` (a resolução
    legada do checkout); de um diretório qualquer, `harness.paths` manda o dado
    para o home — que é o que impede `git -C <repo>` de resolver `data` dentro
    do repo do usuário.
    """
    monkeypatch.delenv("HARNESS_DATA_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HARNESS_HOME", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    ws = prov.provision(repo, "run-rel", config_path=CONFIG)

    assert ws.path == home / "data" / "ws" / "run-rel"
    assert (ws.path / "src" / "app.py").is_file()  # o checkout está onde dizemos
    assert not (repo / "data").exists()  # e o repo alvo fica limpo

    assert prov.provision(repo, "run-rel", config_path=CONFIG).path == ws.path  # idempotente

    prov.dispose(ws, keep=False)
    assert not ws.path.exists()
    assert not (repo / "data").exists()


def test_config_declara_os_cache_links():
    assert prov.cache_links(CONFIG) == ("node_modules", ".venv", ".cache")


def test_cache_links_cai_no_default_sem_arquivo(tmp_path):
    assert prov.cache_links(tmp_path / "nao_existe.toml") == prov.DEFAULT_CACHE_LINKS


def test_bench_provision_imprime_p50_abaixo_de_2s(repo, data_dir, capsys):
    assert cli.main(["bench", "provision", "--n", "3", "--repo", str(repo)]) == 0

    out = capsys.readouterr().out
    m = re.search(r"p50=([0-9.]+)s", out)
    assert m, out
    assert float(m.group(1)) < 2.0
    assert "p95=" in out
    assert "bench-" not in _worktree_list(repo)  # bench não deixa lixo
