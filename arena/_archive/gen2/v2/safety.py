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
    """Resolve a path (following symlinks) and guarantee it stays inside sandbox_root.

    Rejects absolute paths, `..` traversal, and symlinks that point outside the
    sandbox — resolve() follows symlinks before the containment check runs.
    """
    sandbox_root = sandbox_root.resolve()
    if Path(path).is_absolute():
        raise SafetyViolation(f"absolute path not allowed: {path}")
    candidate = (sandbox_root / path)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(sandbox_root)
    except ValueError:
        raise SafetyViolation(f"path escapes sandbox: {path} -> {resolved} not under {sandbox_root}")
    # also guard the parent (covers writes to a not-yet-existing path through a
    # symlinked directory that itself escapes the sandbox)
    try:
        resolved.parent.resolve().relative_to(sandbox_root)
    except ValueError:
        raise SafetyViolation(f"parent escapes sandbox: {path}")
    return resolved


def hash_files(paths):
    """SHA-256 of a set of files, for tamper-evidence on the test oracle."""
    import hashlib
    out = {}
    for p in paths:
        p = Path(p)
        if p.exists():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[str(p)] = None
    return out
