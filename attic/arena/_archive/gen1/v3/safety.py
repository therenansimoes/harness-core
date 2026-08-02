"""Deterministic safety invariants — enforced by code, not by prompt text.

Every file write and every shell command in this harness MUST go through
these two functions. They are the only gate; nothing bypasses them.
"""
import os
import subprocess

ROOT = os.path.abspath(os.path.dirname(__file__))

BLOCKED_SUBSTRINGS = [
    "rm -rf", "git push", "git commit", "sudo", "curl ", "wget ",
    "ssh ", "scp ", ":(){ :|:& };:", "mkfs", "dd if=", "> /dev/",
    "chmod -R 777 /", "chown -R",
]


class SafetyViolation(Exception):
    pass


def guard_path(path: str) -> str:
    """Reject any path that resolves outside ROOT."""
    abspath = os.path.abspath(os.path.join(ROOT, path))
    if not (abspath == ROOT or abspath.startswith(ROOT + os.sep)):
        raise SafetyViolation(f"path escapes sandbox root: {path} -> {abspath}")
    return abspath


def safe_write(path: str, content: str) -> None:
    target = guard_path(path)
    with open(target, "w") as f:
        f.write(content)


def safe_read(path: str) -> str:
    target = guard_path(path)
    with open(target) as f:
        return f.read()


def guard_command(cmd: str) -> None:
    low = cmd.lower()
    for bad in BLOCKED_SUBSTRINGS:
        if bad in low:
            raise SafetyViolation(f"blocked command pattern: {bad!r} in {cmd!r}")


def safe_run(cmd: list, cwd: str = ROOT, timeout: int = 30):
    """Run a command as an argv list (no shell=True — no injection surface)."""
    guard_command(" ".join(cmd))
    cwd_abs = guard_path(os.path.relpath(cwd, ROOT)) if cwd != ROOT else ROOT
    try:
        proc = subprocess.run(
            cmd, cwd=cwd_abs, capture_output=True, text=True,
            timeout=timeout, shell=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
