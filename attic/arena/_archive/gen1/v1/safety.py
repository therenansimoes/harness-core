"""Deterministic safety invariants — enforced in code, not prompt text."""
import re
import shlex
from pathlib import Path

BLOCKED_CMD_PATTERNS = [
    r"\brm\s+-rf\s+/", r"\bsudo\b", r"\bcurl\b", r"\bwget\b", r"\bssh\b",
    r"\bscp\b", r"\bgit\s+push\b", r"\bgit\s+commit\b", r"\bnc\b",
    r">\s*/dev/sd", r"\bmkfs\b", r"\bdd\s+if=", r"\bchmod\s+-R\s+777\s+/",
    r":\(\)\{.*\};:",  # fork bomb
]

class SafetyViolation(Exception):
    pass


def check_command(cmd: str):
    for pat in BLOCKED_CMD_PATTERNS:
        if re.search(pat, cmd):
            raise SafetyViolation(f"blocked command pattern matched: {pat!r} in {cmd!r}")
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        raise SafetyViolation(f"unparseable command: {e}")
    if tokens and tokens[0] in {"rm"} and any(t in ("/", "/*", "~", "$HOME") for t in tokens):
        raise SafetyViolation("rm targeting root/home")
    return True


def check_path(path: str, sandbox_root: Path) -> Path:
    """Resolve a path and guarantee it stays inside sandbox_root."""
    sandbox_root = sandbox_root.resolve()
    p = (sandbox_root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        p.relative_to(sandbox_root)
    except ValueError:
        raise SafetyViolation(f"path escapes sandbox: {path} -> {p} not under {sandbox_root}")
    return p
