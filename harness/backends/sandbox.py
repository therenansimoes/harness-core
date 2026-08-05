"""Sandbox de SO por baixo da tool `execute`.

A denylist do safe_shell continua como defesa em profundidade; esta camada é o
limite imposto pelo KERNEL: mesmo o que escapa da regex (subprocess de Python,
path relativo que sobe além do workspace) esbarra aqui. Estratégia por
plataforma: Seatbelt (`sandbox-exec`) no darwin agora; bubblewrap no linux
depois. Fail-open SÓ no setup — sandbox indisponível deixa o run seguir sem
ele; negação em RUNTIME é erro real que o modelo vê e corrige.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from harness.paths import config_file

MODE_OFF = "off"
MODE_WORKSPACE_WRITE = "workspace-write"
VALID_MODES = frozenset({MODE_OFF, MODE_WORKSPACE_WRITE})

NET_DENY = "deny"
NET_LOCALHOST = "localhost"
NET_ALLOW = "allow"
VALID_NETWORKS = frozenset({NET_DENY, NET_LOCALHOST, NET_ALLOW})

CONFIG_FILE_NAME = "tools.toml"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"  # binário do macOS; caminho fixo do sistema

# Escrita em /dev que qualquer comando de shell normal precisa (redirecionar
# pra /dev/null, tty interativo, dtrace helper do próprio SO).
_DEV_WRITE_LITERALS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/dtracehelper")

_log = logging.getLogger("harness.sandbox")


@dataclass(frozen=True)
class SandboxSettings:
    """Configuração lida de [executor] no tools.toml."""

    mode: str = MODE_OFF
    network: str = NET_DENY
    extra_write: tuple[str, ...] = ()


def load_settings(config_path: Path | None = None) -> SandboxSettings:
    """Lê [executor] do tools.toml.

    Ausência do arquivo, erro de parse ou valor inválido => default (fail-open,
    com warning só no valor inválido)."""
    path = config_path or config_file(CONFIG_FILE_NAME)
    if not path.is_file():
        return SandboxSettings()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("tools.toml ilegível (%s); sandbox desligado", exc)
        return SandboxSettings()
    section = data.get("executor", {})
    mode = section.get("sandbox", MODE_OFF)
    network = section.get("sandbox_network", NET_DENY)
    extra = section.get("sandbox_extra_write", [])
    if mode not in VALID_MODES:
        _log.warning("sandbox mode inválido %r; usando 'off'", mode)
        mode = MODE_OFF
    if network not in VALID_NETWORKS:
        _log.warning("sandbox_network inválido %r; usando 'deny'", network)
        network = NET_DENY
    extra_write = tuple(str(p) for p in extra if isinstance(p, str))
    return SandboxSettings(mode=mode, network=network, extra_write=extra_write)


@runtime_checkable
class SandboxStrategy(Protocol):
    """Embrulha um comando de shell para rodar dentro do sandbox da plataforma."""

    name: str

    def wrap(self, command: str) -> str: ...


def generate_profile(write_roots: Sequence[Path], network: str) -> str:
    """Gera o SBPL (texto puro, testável sem tocar o SO).

    Roots são RESOLVIDOS aqui (symlink: /tmp -> /private/tmp — o Seatbelt
    compara paths resolvidos). Root com aspas no path => ValueError (SBPL não
    tem escape confiável; o factory captura e faz fail-open)."""
    resolved = [str(Path(r).resolve()) for r in write_roots]
    if any('"' in p for p in resolved):
        raise ValueError("path com aspas no profile do sandbox")
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
    ]
    lines.extend(f'  (subpath "{p}")' for p in resolved)
    lines.extend(f'  (literal "{d}")' for d in _DEV_WRITE_LITERALS)
    lines.append('  (subpath "/dev/fd")')
    lines.append(")")
    if network == NET_DENY:
        lines.append("(deny network*)")
    elif network == NET_LOCALHOST:
        lines.append("(deny network*)")
        lines.append('(allow network* (remote ip "localhost:*") (local ip "localhost:*"))')
    # NET_ALLOW: nada — (allow default) já cobre.
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class DarwinSeatbeltSandbox:
    """Seatbelt do macOS: reescreve o comando para rodar sob sandbox-exec."""

    profile_path: Path
    name: str = "seatbelt"

    def wrap(self, command: str) -> str:
        profile = shlex.quote(str(self.profile_path))
        return f"{SANDBOX_EXEC} -f {profile} /bin/sh -c {shlex.quote(command)}"


def default_write_roots(workspace: Path) -> list[Path]:
    """Workspace + temp do usuário + cache do usuário.

    pip/clang/uv escrevem cache fora do workspace; sem esses roots ferramenta
    normal quebra dentro do sandbox."""
    roots = [workspace]
    for key in ("CS_DARWIN_USER_TEMP_DIR", "CS_DARWIN_USER_CACHE_DIR"):
        try:
            v = os.confstr(key)
        except (ValueError, OSError):
            v = None
        if v:
            roots.append(Path(v))
    roots.append(Path(tempfile.gettempdir()))
    return roots  # duplicatas são inofensivas no profile


def make_sandbox(
    workspace: Path,
    settings: SandboxSettings,
    *,
    write_roots: Sequence[Path] | None = None,
    platform: str | None = None,
    profile_dir: Path | None = None,
) -> SandboxStrategy | None:
    """Constrói a estratégia de sandbox para a plataforma, ou None.

    Fail-open TOTAL: qualquer falha aqui => warning + None (run sem sandbox).
    NUNCA levanta exceção."""
    try:
        if settings.mode == MODE_OFF:
            return None  # silencioso, é o default
        plat = platform or sys.platform
        if plat != "darwin":
            _log.warning(
                "sandbox '%s' pedido mas só darwin é suportado; rodando sem sandbox",
                settings.mode,
            )
            return None
        if not (os.path.exists(SANDBOX_EXEC) or shutil.which("sandbox-exec")):
            _log.warning("sandbox-exec não encontrado; rodando sem sandbox")
            return None
        roots = list(write_roots) if write_roots is not None else default_write_roots(workspace)
        roots += [Path(p) for p in settings.extra_write]
        profile = generate_profile(roots, settings.network)
        # NUNCA dentro do workspace: o snapshot_diff do run detectaria o
        # profile como arquivo escrito.
        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)
            path = profile_dir / "harness-sandbox.sb"
            path.write_text(profile, encoding="utf-8")
        else:
            fd, name = tempfile.mkstemp(prefix="harness-sbx-", suffix=".sb")
            os.write(fd, profile.encode("utf-8"))
            os.close(fd)
            path = Path(name)
        _log.info("sandbox ativo: seatbelt (network=%s) profile=%s", settings.network, path)
        return DarwinSeatbeltSandbox(profile_path=path)
    except Exception as exc:
        _log.warning("sandbox indisponível (%s); rodando sem sandbox", exc)
        return None


__all__ = [
    "CONFIG_FILE_NAME",
    "MODE_OFF",
    "MODE_WORKSPACE_WRITE",
    "NET_ALLOW",
    "NET_DENY",
    "NET_LOCALHOST",
    "SANDBOX_EXEC",
    "VALID_MODES",
    "VALID_NETWORKS",
    "DarwinSeatbeltSandbox",
    "SandboxSettings",
    "SandboxStrategy",
    "default_write_roots",
    "generate_profile",
    "load_settings",
    "make_sandbox",
]
