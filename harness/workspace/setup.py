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
import os
import subprocess
import time
from pathlib import Path

from harness.ledger.store import data_dir
from harness.projects import Project

SETUP_SUBDIR = "setup"
LOG_FILE = Path(".harness") / "setup.log"
ENV_FILE_PTR = Path(".harness") / "env_file"
PYTHON_VERSION_FILE = ".python-version"
RC_TIMEOUT = 124  # mesmo rc que o `timeout(1)` usa
RC_NO_PYTHON = 125  # versão de python do projeto não instalada na máquina
PIN_TIMEOUT = 30  # `uv python find/pin` é local; mais que isso é uv travado
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


def write_env_pointer(ws: Path, proj: Project) -> Path | None:
    """Grava `<ws>/.harness/env_file` com o path do env do projeto.

    É o único vínculo ws→projeto que as tools têm: `flow_tools` e `procs` só
    recebem o workspace, e ler um ponteiro é mais barato que carregar o
    registro de projetos a cada subprocess.
    """
    from harness.projects import env_file_path

    alvo = env_file_path(proj, ws)
    ptr = Path(ws) / ENV_FILE_PTR
    try:
        if alvo is None:
            ptr.unlink(missing_ok=True)  # projeto perdeu o env_file: ponteiro sai
            return None
        ptr.parent.mkdir(parents=True, exist_ok=True)
        ptr.write_text(str(alvo), encoding="utf-8")
    except OSError:  # ponteiro é conveniência, não pode derrubar o setup
        return None
    return alvo


def pin_python(ws: Path) -> tuple[bool, str]:
    """Fixa o interpretador do workspace no `.python-version` do projeto.

    `(ok, detalhe)`. Sem `.python-version` não há nada a fazer (`ok=True`).
    A versão TEM de já estar instalada: `uv python install` baixa toolchain
    (centenas de MB, minutos) e o setup não é lugar de decidir isso — ausente,
    devolve `ok=False` com a instrução, e quem chama registra `setup_failed`.
    """
    ws = Path(ws)
    f = ws / PYTHON_VERSION_FILE
    if not f.is_file():
        return True, ""
    try:
        conteudo = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return True, f"{PYTHON_VERSION_FILE} ilegível ({exc}), pin ignorado"
    versao = next(
        (
            ln.strip()
            for ln in conteudo.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ),
        "",
    )
    if not versao:
        return True, f"{PYTHON_VERSION_FILE} vazio, pin ignorado"

    # `UV_PYTHON_DOWNLOADS=never`: o find falha em vez de puxar toolchain.
    env = {**os.environ, "UV_PYTHON_DOWNLOADS": "never"}
    try:
        achou = subprocess.run(
            ["uv", "python", "find", versao],
            cwd=ws, capture_output=True, text=True, timeout=PIN_TIMEOUT, env=env,
        )
    except FileNotFoundError:
        return True, "uv ausente, pin ignorado"
    except subprocess.TimeoutExpired:
        return True, f"`uv python find {versao}` excedeu {PIN_TIMEOUT}s, pin ignorado"
    if achou.returncode != 0:
        return False, (
            f"python {versao} (de {PYTHON_VERSION_FILE}) não instalado nesta máquina — "
            f"rode `uv python install {versao}` e refaça o run; o setup NÃO baixa "
            "toolchain sozinho"
        )

    try:
        pin = subprocess.run(
            ["uv", "python", "pin", versao],
            cwd=ws, capture_output=True, text=True, timeout=PIN_TIMEOUT, env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return True, f"pin falhou ({type(exc).__name__}), segue com {versao} presente"
    detalhe = (pin.stdout + pin.stderr).strip().splitlines()
    return True, f"python {versao} pinado ({achou.stdout.strip()})" + (
        f" — {detalhe[-1]}" if pin.returncode != 0 and detalhe else ""
    )


def ensure(ws: Path, proj: Project) -> dict:
    """Garante o ambiente do workspace. `{"skipped", "sec", "ok"}`.

    `skipped=True` é o caminho quente (cache válido) e também o caso sem
    comando nenhum a rodar. `ok=False` só reporta — quem chama decide.
    """
    ws = Path(ws)
    t0 = time.monotonic()
    write_env_pointer(ws, proj)
    pin_ok, pin_detalhe = pin_python(ws)
    if pin_detalhe:
        _log(ws, f"$ uv python pin\n{pin_detalhe}\nrc={0 if pin_ok else RC_NO_PYTHON}\n")
    if not pin_ok:
        # Venv com o python errado é pior que venv nenhum: para aqui e o trace
        # carrega `setup_failed` com a instrução já no log.
        return {"skipped": False, "sec": time.monotonic() - t0, "ok": False}
    cmd = proj.setup_cmd or detect_cmd(ws)
    if not cmd:
        # `sec=0.0` literal: sem comando não houve instalação, e o custo do pin
        # (subprocess local) não é o número que o trace mede.
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
            rc = _run(ws, cmd, proj.setup_timeout, _setup_env(ws, proj))
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


def _run(ws: Path, cmd: str, timeout: int, env: dict[str, str] | None = None) -> int:
    """Roda o setup no workspace, log em `<ws>/.harness/setup.log` (append: o
    histórico de tentativas fica junto do run que as fez)."""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=ws, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        out, rc = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        out, rc = f"setup excedeu {timeout}s\n", RC_TIMEOUT
    _log(ws, f"$ {cmd}\n{out}\nrc={rc}\n")
    return rc


def _log(ws: Path, texto: str) -> None:
    """Append no `setup.log` com redação: `npm ci` de registry privado ecoa
    `//registry/:_authToken`, e esse log vai para o relatório do run."""
    from harness.redact import redact

    log = ws / LOG_FILE
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(redact(texto))
    except OSError:  # log é evidência, não pré-requisito
        pass


def _setup_env(ws: Path, proj: Project) -> dict[str, str] | None:
    """Env do comando de setup: o do processo + o do projeto (index privado
    costuma exigir token). Var já exportada ganha do arquivo."""
    from harness.projects import project_env

    do_projeto = project_env(proj, ws)
    if not do_projeto:
        return None
    return {**do_projeto, **os.environ}
