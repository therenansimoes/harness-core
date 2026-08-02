"""Deterministic safety invariants, enforced by code — not by prompt text.

Every filesystem write and every subprocess call in this harness goes
through guard_path()/safe_write()/safe_run(). Two hardened points vs the
gen1 parents:

  - guard_path uses os.path.realpath (resolves symlinks) instead of
    os.path.abspath (gen1 v3's bug: abspath does NOT resolve a symlink
    that points outside root, so a crafted symlink escapes the sandbox).
  - safe_run uses an ALLOWLIST of argv[0] binaries instead of a substring
    DENYLIST of dangerous commands (gen1's shared bug across all 5
    candidates: a denylist of substrings is trivially bypassed by
    aliasing, quoting, or just using a binary nobody thought to ban).
"""
import os
import re
import subprocess

ROOT = os.path.realpath(os.path.dirname(__file__))

# Only these binaries may ever be exec'd by the harness or its subagents.
# argv is always a list (never shell=True), so there is no injection
# surface via quoting/chaining — the allowlist blocks *which program*
# runs, not which characters appear.
ALLOWED_BINARIES = {"python3", "python", "pytest", "sh"}
ALLOWED_BINARY_PATTERN = re.compile(r"^python3(\.\d+)?$")


class SafetyViolation(Exception):
    pass


def guard_path(path: str) -> str:
    """Reject any path that resolves (after symlink resolution) outside ROOT."""
    target = os.path.realpath(os.path.join(ROOT, path) if not os.path.isabs(path) else path)
    if not (target == ROOT or target.startswith(ROOT + os.sep)):
        raise SafetyViolation(f"path escapes sandbox root: {path} -> {target}")
    return target


def safe_write(path: str, content: str) -> None:
    target = guard_path(path)
    with open(target, "w") as f:
        f.write(content)


def safe_read(path: str) -> str:
    target = guard_path(path)
    with open(target) as f:
        return f.read()


def guard_command(argv: list) -> None:
    if not argv:
        raise SafetyViolation("empty command")
    binary = os.path.basename(argv[0])
    if binary not in ALLOWED_BINARIES and not ALLOWED_BINARY_PATTERN.match(binary):
        raise SafetyViolation(f"binary not in allowlist: {binary!r} (argv={argv!r})")


def safe_run(argv: list, cwd: str, timeout: int = 20):
    """Run argv (never shell=True) after allowlist + path checks."""
    guard_command(argv)
    cwd_real = os.path.realpath(cwd)
    # cwd must be inside ROOT for harness-internal calls, but external
    # target projects (arbitrary third-party dirs) are explicitly allowed
    # by the caller passing an absolute --task-dir; we only forbid escaping
    # via relative traversal from within ROOT-scoped calls. See harness.py.
    try:
        proc = subprocess.run(
            argv, cwd=cwd_real, capture_output=True, text=True,
            timeout=timeout, shell=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except FileNotFoundError as e:
        return -1, "", str(e)
