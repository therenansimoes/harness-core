"""Provisionamento de workspace por run — o ataque ao throughput.

`git worktree add --detach` no lugar de `shutil.copytree`: o custo deixa de ser
proporcional ao tamanho do repo. Cache pesado (node_modules, .venv) entra por
symlink, listado em `config/tools.toml` — nunca hardcoded aqui.

Workspaces vivem em `$HARNESS_DATA_DIR/ws/<run_id>` (mesma raiz do ledger).
`dispose` só apaga dentro dessa raiz; fora dela, recusa.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from harness.ledger.store import data_dir

Mode = Literal["worktree", "tmpdir"]

WS_SUBDIR = "ws"
CONFIG_FILE = Path("config/tools.toml")
DEFAULT_CACHE_LINKS: tuple[str, ...] = ("node_modules", ".venv", ".cache")


@dataclass(frozen=True)
class Workspace:
    """Onde o run acontece. `repo` é a origem, para o dispose saber o que remover."""

    path: Path
    run_id: str
    mode: Mode
    repo: Path


def ws_root() -> Path:
    """Sempre absoluto: `git -C <repo> worktree add data/ws/x` resolveria o path
    relativo dentro do repo alvo, não no cwd do processo."""
    return (data_dir() / WS_SUBDIR).resolve()


def workspace_path(run_id: str) -> Path:
    return ws_root() / run_id


def cache_links(config_path: Path | None = None) -> tuple[str, ...]:
    """`[workspace] cache_links` do tools.toml; defaults se o arquivo não existir."""
    path = config_path or CONFIG_FILE
    if not path.is_file():
        return DEFAULT_CACHE_LINKS
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    names = data.get("workspace", {}).get("cache_links", DEFAULT_CACHE_LINKS)
    return tuple(str(n) for n in names)


def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    proc = _git(path, "rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def provision(
    repo: Path,
    run_id: str,
    mode: Mode = "worktree",
    config_path: Path | None = None,
) -> Workspace:
    """Prepara o workspace do run. Idempotente: mesmo `run_id` reusa o que existe."""
    repo = Path(repo).resolve()
    path = workspace_path(run_id)
    names = cache_links(config_path)

    if mode == "worktree":
        _add_worktree(repo, path)
    elif mode == "tmpdir":
        _copy_tree(repo, path, names)
    else:
        raise ValueError(f"mode desconhecido: {mode!r}")

    _link_caches(repo, path, names)
    return Workspace(path=path, run_id=run_id, mode=mode, repo=repo)


def dispose(ws: Workspace, keep: bool = False) -> None:
    """Descarta o workspace. `keep=True` deixa tudo no lugar (inspeção humana)."""
    if keep:
        return
    _assert_inside_ws_root(ws.path)
    if ws.mode == "worktree" and is_git_repo(ws.repo):
        _git(ws.repo, "worktree", "remove", "--force", str(ws.path))
        _git(ws.repo, "worktree", "prune")
    if ws.path.exists():
        # tmpdir, ou worktree que o git já não reconhece. rmtree não segue
        # symlink: o cache da origem fica intacto.
        shutil.rmtree(ws.path)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Chamada de git que nunca levanta — quem chama decide o que fazer com o rc."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )


def _worktrees(repo: Path) -> set[Path]:
    proc = _git(repo, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        return set()
    prefix = "worktree "
    return {
        Path(line[len(prefix) :]).resolve()
        for line in proc.stdout.splitlines()
        if line.startswith(prefix)
    }


def _is_tracked(repo: Path, name: str) -> bool:
    return bool(_git(repo, "ls-files", "--", name).stdout.strip())


def _add_worktree(repo: Path, path: Path) -> None:
    if not is_git_repo(repo):
        raise ValueError(f"{repo} não é repositório git — use mode='tmpdir'")
    if path.exists():
        if path.resolve() in _worktrees(repo):
            return
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()  # sobra vazia de um dispose anterior
        else:
            raise ValueError(f"{path} existe e não é worktree de {repo}")

    path.parent.mkdir(parents=True, exist_ok=True)
    proc = _git(repo, "worktree", "add", "--detach", str(path))
    if proc.returncode != 0:
        # registro órfão (workspace apagado na mão): limpa e tenta uma vez.
        _git(repo, "worktree", "prune")
        proc = _git(repo, "worktree", "add", "--detach", str(path))
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add falhou: {proc.stderr.strip()}")


def _copy_tree(repo: Path, path: Path, names: Iterable[str]) -> None:
    """Fallback para repo não-git. Ignora `.git` e os caches (que viram symlink)."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        repo, path, symlinks=True, ignore=shutil.ignore_patterns(".git", *names)
    )


def _link_caches(repo: Path, ws: Path, names: Iterable[str]) -> None:
    """Symlinka cache que existe na origem e não está versionado."""
    for name in names:
        src = repo / name
        dst = ws / name
        if not src.exists() or _is_tracked(repo, name):
            continue
        if dst.is_symlink() or dst.exists():
            continue  # nada do checkout é sobrescrito
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src.resolve(), target_is_directory=src.is_dir())


def _assert_inside_ws_root(path: Path) -> None:
    """Guarda-corpo: nenhum rm fora de `$HARNESS_DATA_DIR/ws`."""
    root = ws_root()
    target = path.resolve()
    if root not in target.parents:
        raise ValueError(f"recusado: {target} está fora de {root}")
