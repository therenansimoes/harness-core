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
        re.compile(r"\brm\b[^;&|]*-[a-zA-Z]*[rf][a-zA-Z]*\s+(-\S+\s+)*[/~*]|\brm\b[^;&|]*\s\.\.?(\s|$)"),
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
    (
        re.compile(r"\b(pip3?|uv|npm|pnpm|yarn|cargo|gem|apt|apt-get)\s+(install|add|sync)\b"),
        "instalar dependência (o run não instala nada)",
    ),
    (re.compile(r">\s*/dev/(disk|rdisk|sd[a-z])"), "escrita em dispositivo"),
    (re.compile(r"\bhistory\s+-c\b|\bshred\b"), "apagar rastro"),
)

# Token com cara de caminho absoluto. O lookbehind evita falso positivo em
# `s/foo/bar/` (sed), `https://x` e continuação de path já casado.
_ABSOLUTE_TOKEN = re.compile(r"(?<![A-Za-z0-9_.:$/=-])/[A-Za-z0-9_.\-][A-Za-z0-9_.\-/]*")


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
        return super().execute(command, timeout=timeout)

    def _blocked_reason(self, command: str) -> str | None:
        """Nunca propaga exceção: cerca que derruba o run é pior que cerca que
        deixa passar — e o que passa ainda roda com cwd no workspace."""
        try:
            return check_command(command, Path(self.cwd))
        except Exception:
            return None


__all__ = ["DEFAULT_TIMEOUT", "DENYLIST", "MAX_TIMEOUT", "SafeShellBackend", "check_command"]
