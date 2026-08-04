"""Cerca do shell do executor.

O `LocalShellBackend` do deepagents é shell REAL sem sandbox: `virtual_mode`
só protege as tools de arquivo, e a docstring dele diz na cara que
`execute("cat /etc/passwd")` funciona. O harness roda modelos locais em loop
autônomo, então a cerca é aqui: comando harmful ou que sai do workspace é
recusado ANTES do subprocess, e a recusa volta como output normal da tool —
o modelo lê o motivo, aprende a regra e tenta de novo dentro dela.

Fail-closed no que é destrutivo (denylist), fail-open no run: bloqueio NUNCA
levanta exceção que derruba a execução.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import replace
from pathlib import Path

from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

# Timeout por comando. O default do deepagents é 120s; um run do harness tem
# orçamento de minutos, comando pendurado é o jeito mais barato de queimá-lo.
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120

BLOCKED_EXIT_CODE = 126  # "command found but not executable", em espírito

_PREFIX = "comando bloqueado pela cerca do harness"
_HINT = "use paths relativos ao workspace"

# `mv`, `mkdir -p`, `ruff format` sem achado: sucesso silencioso. Output vazio é
# indistinguível de "a tool falhou" para o modelo pequeno, que reexecuta o mesmo
# comando e queima turno. Dizer o sucesso em texto mata a ambiguidade.
EMPTY_OUTPUT = "(comando executou com exit 0 e não produziu saída)"
# O `LocalShellBackend` já troca saída vazia por este placeholder (local_shell.py
# ~325), então o vazio quase nunca chega cru aqui — os dois contam como vazio.
_LIB_EMPTY_OUTPUT = "<no output>"

# Absolutos inofensivos: `2>/dev/null` aparece em quase todo comando de verify
# e não é vazamento de workspace.
ABSOLUTE_ALLOWLIST = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")

# (regex, motivo). Ordem = ordem de checagem; a primeira que casa é o motivo.
DENYLIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r":\s*\(\s*\)\s*\{"), "forkbomb"),
    (re.compile(r"\bsudo\b|\bsu\s+-\b|\bdoas\b"), "escalação de privilégio (sudo)"),
    (
        re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
        "desligar/reiniciar a máquina",
    ),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bdiskutil\b|\bfdisk\b"), "formatar disco"),
    (re.compile(r"\bdd\b\s+(if|of)="), "dd (escrita crua em dispositivo)"),
    # `rm -rf build/` é legítimo; `rm -rf /`, `~`, `*`, `.` e `..` não.
    (
        re.compile(
            r"\brm\b[^;&|]*-[a-zA-Z]*[rf][a-zA-Z]*\s+(-\S+\s+)*[/~*]|\brm\b[^;&|]*\s\.\.?(\s|$)"
        ),
        "rm recursivo em raiz, home ou glob solto",
    ),
    (re.compile(r"\b(chmod|chown|chgrp)\b[^;&|]*\s/"), "chmod/chown fora do workspace"),
    (re.compile(r"\b(kill|killall|pkill)\b"), "matar processo (o harness é um deles)"),
    (
        re.compile(r"\b(curl|wget)\b[^;&|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b"),
        "download encanado em shell",
    ),
    (re.compile(r"\bgit\s+push\b"), "git push (publicar é decisão do dono)"),
    (re.compile(r"\bgit\s+(remote\s+add|clone)\b"), "git remote/clone"),
    (re.compile(r"\b(ssh|scp|sftp|rsync)\b"), "acesso a máquina remota"),
    (re.compile(r"\bcrontab\b|\blaunchctl\b|\bsystemctl\b"), "agendador/serviço do sistema"),
    (re.compile(r"\bosascript\b|\bopen\s+-a\b|\bdefaults\s+write\b"), "automação do macOS"),
    # O `ollama` fica na cerca mesmo com o runtime cortado em 2026-08-04: o
    # binário pode existir na máquina, e cercar é sobre o que o run PODE fazer.
    (re.compile(r"\bollama\b|\blms\b|\bmlx_lm\b"), "controlar o servidor de modelo"),  # cortado
    (re.compile(r"\bbrew\b"), "brew (o run não instala nada)"),
    # Instalar NO workspace é permitido (é assim que o run resolve dependência);
    # o que fica fora é o que instala na máquina. `installer_reason` cuida do
    # caso fino (flag global, pip fora do venv); aqui só o que nunca é local.
    (re.compile(r"\bpipx\b"), "pipx (instala na máquina, não no workspace)"),
    (
        re.compile(r"\b(gem|apt|apt-get|port|snap)\s+install\b"),
        "gerenciador de pacote do sistema",
    ),
    # Container é serviço da máquina, não do workspace: entra pelo slot
    # `services_cmd`, não pelo shell do run. NÃO é regex de palavra — bloquear
    # `\bdocker\b` recusaria `cat docker-compose.yml`, que é leitura inocente;
    # a checagem é por POSIÇÃO de comando, em `_segment_reason`.
    (re.compile(r">\s*/dev/(disk|rdisk|sd[a-z])"), "escrita em dispositivo"),
    (re.compile(r"\bhistory\s+-c\b|\bshred\b"), "apagar rastro"),
)

# Token com cara de caminho absoluto. O lookbehind evita falso positivo em
# `s/foo/bar/` (sed), `https://x` e continuação de path já casado.
_ABSOLUTE_TOKEN = re.compile(r"(?<![A-Za-z0-9_.:$/=-])/[A-Za-z0-9_.\-][A-Za-z0-9_.\-/]*")

# Instalador é permitido dentro do workspace e bloqueado quando escreve fora
# dele. Estas constantes são a lista do que a cerca sabe reconhecer.
INSTALLERS = {"pip", "pip3", "uv", "uvx", "npm", "pnpm", "yarn", "bun", "cargo"}
INSTALL_SUBCMDS = {
    "install",
    "i",
    "ci",
    "add",
    "sync",
    "venv",
    "remove",
    "uninstall",
    "pip",
}
# Flags que tiram a instalação do workspace (global, home, prefixo, cache
# compartilhado). `--x=valor` casa pelo prefixo `--x=`.
GLOBAL_FLAGS = {
    "-g",
    "--global",
    "--location=global",
    "--user",
    "--prefix",
    "--target",
    "-t",
    "--root",
    "--system",
    "--cache-dir",
    "--cache",
    "--store-dir",
}

# Runtime de container: serviço da máquina, não do workspace. Checado por
# posição (prog do segmento), nunca por palavra solta — `cat docker-compose.yml`
# menciona `docker` e é leitura inocente.
CONTAINER_PROGS = {"docker", "docker-compose", "podman", "podman-compose"}
_CONTAINER_REASON = "docker/podman (serviço da máquina; slot services_cmd futuro)"

# Separador de comando: o segmento seguinte tem prog/sub próprios.
_SEGMENT_SEPARATORS = set(";|&()<>")
# shlex quebrado não deve derrubar a checagem — mas `-g`/`--global` é grosseiro
# o bastante para ser pego no texto cru.
_RAW_GLOBAL = re.compile(r"\B-g\b|\B--global\b")
_PIP_VENV_HINT = (
    "pip fora do venv do workspace; use install_deps ou uv pip install --python .venv/bin/python"
)


def installer_reason(command: str, root: Path) -> str | None:
    """Motivo do bloqueio quando o comando instala FORA do workspace.

    Também recusa runtime de container (`docker`/`podman`) quando ele é o
    PROGRAMA do segmento — mencionar o nome em argumento não conta.

    Pura. Instalar dentro do workspace (`uv sync`, `npm ci`, `uv pip install
    -e .`) é o jeito normal de o run resolver dependência; o que a cerca recusa
    é instalação global — flag `-g`/`--user`/`--prefix`, cache compartilhado,
    `pip` do sistema em vez do venv, `uv venv` apontado para fora."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # Quote/escape aberto: fail-open, exceto no caso grosseiro.
        if _RAW_GLOBAL.search(command):
            return "instalador global/fora do workspace (-g/--global)"
        return None
    for segment in _segments(tokens):
        reason = _segment_reason(segment, root)
        if reason:
            return reason
    return None


def _segments(tokens: list[str]) -> list[list[str]]:
    """Quebra a lista de tokens em segmentos de comando (`;`, `&&`, `||`, `|`)."""
    out: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _SEGMENT_SEPARATORS:
            out.append([])
        else:
            out[-1].append(token)
    return [segment for segment in out if segment]


def _segment_reason(segment: list[str], root: Path) -> str | None:
    words = list(segment)
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words.pop(0)  # `FOO=bar npm ...`: a atribuição não é o programa
    if words and words[0].rsplit("/", 1)[-1] in CONTAINER_PROGS:
        return _CONTAINER_REASON
    if len(words) < 2:
        return None
    prog, sub = words[0], words[1]
    name = prog.rsplit("/", 1)[-1]
    if name not in INSTALLERS or sub not in INSTALL_SUBCMDS:
        return None
    args = words[2:]
    for flag in [sub, *args]:
        if flag in GLOBAL_FLAGS or ("=" in flag and flag.split("=", 1)[0] in GLOBAL_FLAGS):
            return f"instalador global/fora do workspace ({flag})"
    # `uv pip` e `.venv/bin/pip` já apontam para o venv do workspace; `pip`
    # solto é o do sistema até provar o contrário com `--python`.
    if name.startswith("pip") and "/" not in prog and not _has_local_python(args, root):
        return _PIP_VENV_HINT
    if name == "uv" and sub == "venv":
        target = next((a for a in args if not a.startswith("-")), None)
        if target and target.startswith("/") and not _inside(target, root):
            return f"uv venv fora do workspace ({target})"
    return None


def _has_local_python(args: list[str], root: Path) -> bool:
    """True se `--python`/`-p` aponta para dentro do workspace."""
    for index, arg in enumerate(args):
        value: str | None = None
        if arg in ("--python", "-p"):
            value = args[index + 1] if index + 1 < len(args) else None
        elif arg.startswith("--python="):
            value = arg.split("=", 1)[1]
        if value:
            return not value.startswith("/") or _inside(value, root)
    return False


def check_command(command: str, workspace: Path) -> str | None:
    """Motivo do bloqueio, ou None se o comando pode rodar.

    Pura de propósito: a decisão da cerca é testável sem subir agente nenhum.
    """
    if not isinstance(command, str) or not command.strip():
        return None  # o próprio LocalShellBackend já devolve erro para isso
    for pattern, reason in DENYLIST:
        if pattern.search(command):
            return reason
    try:
        root = workspace.resolve()
    except OSError:  # workspace sumiu no meio do run: nada é "dentro" dele
        return "workspace inacessível"
    # Antes do token absoluto: motivo específico de instalador é mais útil ao
    # modelo que "caminho absoluto fora do workspace".
    reason = installer_reason(command, root)
    if reason:
        return reason
    for token in _ABSOLUTE_TOKEN.findall(command):
        if token in ABSOLUTE_ALLOWLIST:
            continue
        if not _inside(token, root):
            return f"caminho absoluto fora do workspace ({token})"
    return None


def _inside(token: str, root: Path) -> bool:
    """True se o caminho absoluto aponta para dentro do workspace resolvido.

    Compara o caminho resolvido (segue symlink: `/tmp` no macOS é
    `/private/tmp`, e o workspace do run mora lá)."""
    try:
        candidate = Path(token).resolve()
    except (OSError, ValueError):
        return False
    return candidate == root or root in candidate.parents


def _output_explicito(response):
    """Sucesso silencioso (rc=0, stdout+stderr vazios) ganha texto de sucesso.

    Só toca esse caso: rc != 0 sem saída continua como veio (ali o vazio é o
    dado, e o exit code já diz que falhou). Fail-open no formato do response —
    `ExecuteResponse` é frozen, e resposta de outro shape volta intacta."""
    try:
        output = (response.output or "").strip()
        if response.exit_code == 0 and output in ("", _LIB_EMPTY_OUTPUT):
            return replace(response, output=EMPTY_OUTPUT)
    except (AttributeError, TypeError):
        return response
    return response


class SafeShellBackend(LocalShellBackend):
    """LocalShellBackend com denylist, cerca de workspace e timeout curto.

    Só `execute` muda; as tools de arquivo continuam as do pai (já contidas
    pelo `virtual_mode`)."""

    def __init__(self, *args, timeout: int = DEFAULT_TIMEOUT, **kwargs) -> None:
        super().__init__(*args, timeout=timeout, **kwargs)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        reason = self._blocked_reason(command)
        if reason:
            return ExecuteResponse(
                output=f"{_PREFIX}: {reason}; {_HINT}",
                exit_code=BLOCKED_EXIT_CODE,
                truncated=False,
            )
        # Teto no timeout pedido pelo modelo: ele pede 600s quando um comando
        # trava, e o run não tem esse tempo.
        if timeout is not None:
            timeout = max(1, min(timeout, MAX_TIMEOUT))
        return _output_explicito(super().execute(command, timeout=timeout))

    def _blocked_reason(self, command: str) -> str | None:
        """Nunca propaga exceção: cerca que derruba o run é pior que cerca que
        deixa passar — e o que passa ainda roda com cwd no workspace."""
        try:
            return check_command(command, Path(self.cwd))
        except Exception:
            return None


__all__ = [
    "CONTAINER_PROGS",
    "DEFAULT_TIMEOUT",
    "DENYLIST",
    "EMPTY_OUTPUT",
    "GLOBAL_FLAGS",
    "INSTALLERS",
    "INSTALL_SUBCMDS",
    "MAX_TIMEOUT",
    "SafeShellBackend",
    "check_command",
    "installer_reason",
]
