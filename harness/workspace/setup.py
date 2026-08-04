"""Fase de setup do workspace: dependência instalada uma vez por lockfile.

O executor não deveria gastar turno rodando `npm ci`. O setup roda antes dele,
fora do orçamento do modelo, e é cacheado pelo hash dos lockfiles do workspace
mais o próprio comando: mudou o lockfile, reinstala; não mudou, skipa.

O flock é o que faz o cache virar feature em vez de corrida. `provision`
symlinka `node_modules`/`.venv` DO REPO DE ORIGEM: dois runs paralelos do mesmo
projeto instalam no MESMO diretório físico. Serializado por projeto, o segundo
run chega depois do primeiro terminar, acha o hash igual e nem instala.

Nada aqui levanta: setup é fail-open (`ok=False` no retorno) porque o executor
ainda pode consertar o ambiente por conta própria.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import time
from pathlib import Path

from harness.ledger.store import data_dir
from harness.projects import Project

SETUP_SUBDIR = "setup"
LOG_FILE = Path(".harness") / "setup.log"
RC_TIMEOUT = 124  # mesmo rc que o `timeout(1)` usa
# Ordem fixa: o hash tem de ser estável entre runs, não depender de iterdir.
LOCKFILES: tuple[str, ...] = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "requirements.txt",
    "pyproject.toml",
)


def setup_dir() -> Path:
    return data_dir() / SETUP_SUBDIR


def stamp_path(name: str) -> Path:
    return setup_dir() / f"{name}.json"


def lock_hash(ws: Path, setup_cmd: str | None = None) -> str:
    """Identidade do ambiente: bytes dos lockfiles presentes + o comando.

    Lockfile ausente não entra (nem como vazio): adicionar um `uv.lock` muda o
    hash, e é exatamente aí que reinstalar é obrigatório.
    """
    h = hashlib.sha256()
    for name in LOCKFILES:
        f = Path(ws) / name
        if not f.is_file():
            continue
        h.update(name.encode("utf-8"))
        h.update(f.read_bytes())
    h.update((setup_cmd or "").encode("utf-8"))
    return h.hexdigest()


def detect_cmd(ws: Path) -> str | None:
    """Detecção mínima para projeto que não declara `setup_cmd`. Sem manifesto
    reconhecido devolve None — não inventamos comando em repo alheio."""
    ws = Path(ws)
    if (ws / "package-lock.json").is_file():
        return "npm ci"
    if (ws / "package.json").is_file():
        return "npm install"
    if (ws / "pyproject.toml").is_file():
        return "uv sync"
    if (ws / "requirements.txt").is_file():
        return "uv pip install -r requirements.txt"
    return None


def stack_marker(ws: Path) -> Path | None:
    """Onde a dependência instalada aparece. Stamp sem marker é stamp mentindo:
    alguém apagou o `node_modules` da origem e o cache tem de reinstalar."""
    ws = Path(ws)
    if (ws / "package.json").is_file():
        return ws / "node_modules"
    if (ws / "pyproject.toml").is_file() or (ws / "requirements.txt").is_file():
        return ws / ".venv"
    return None


def ensure(ws: Path, proj: Project) -> dict:
    """Garante o ambiente do workspace. `{"skipped", "sec", "ok"}`.

    `skipped=True` é o caminho quente (cache válido) e também o caso sem
    comando nenhum a rodar. `ok=False` só reporta — quem chama decide.
    """
    ws = Path(ws)
    t0 = time.monotonic()
    cmd = proj.setup_cmd or detect_cmd(ws)
    if not cmd:
        return {"skipped": True, "sec": 0.0, "ok": True}

    digest = lock_hash(ws, cmd)
    stamp = stamp_path(proj.name)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    lock = stamp.with_suffix(".lock")
    with lock.open("a+", encoding="utf-8") as fh:
        # Bloqueante de propósito: o run paralelo espera e reaproveita, em vez
        # de instalar em cima do mesmo diretório symlinkado.
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            if _fresh(stamp, digest, ws):
                return {"skipped": True, "sec": time.monotonic() - t0, "ok": True}
            rc = _run(ws, cmd, proj.setup_timeout)
            if rc == 0:
                # Stamp só no sucesso: setup quebrado tem de tentar de novo.
                stamp.write_text(
                    json.dumps({"hash": digest, "cmd": cmd, "at": time.time()}),
                    encoding="utf-8",
                )
            return {"skipped": False, "sec": time.monotonic() - t0, "ok": rc == 0}
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _fresh(stamp: Path, digest: str, ws: Path) -> bool:
    if not stamp.is_file():
        return False
    try:
        saved = json.loads(stamp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if saved.get("hash") != digest:
        return False
    marker = stack_marker(ws)
    return marker is None or marker.exists()


def _run(ws: Path, cmd: str, timeout: int) -> int:
    """Roda o setup no workspace, log em `<ws>/.harness/setup.log` (append: o
    histórico de tentativas fica junto do run que as fez)."""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=ws, capture_output=True, text=True, timeout=timeout
        )
        out, rc = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        out, rc = f"setup excedeu {timeout}s\n", RC_TIMEOUT
    log = ws / LOG_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {cmd}\n{out}\nrc={rc}\n")
    return rc
