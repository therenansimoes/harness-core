"""Deterministic safety invariants — enforced by code, not by prompt text.

Every file write/read and every shell command in this harness MUST go
through these functions. Fixes inherited from gen1/v3's known holes:
  - guard_path uses realpath (not abspath) so a symlink can't escape ROOT.
  - guard_command is an ALLOWLIST of argv[0] binaries, not a denylist of
    substrings (denylist missed `python3 -c "..."`, which the harness
    itself uses).
"""
import hashlib
import os
import subprocess

ROOT = os.path.realpath(os.path.dirname(__file__))

# Only these interpreters/binaries may ever be exec'd by this harness.
ALLOWED_BINS = {"python3", "python", os.path.realpath(__import__("sys").executable)}


class SafetyViolation(Exception):
    pass


def guard_path(path: str, root: str = ROOT) -> str:
    """Reject any path that resolves (after following symlinks) outside root."""
    root = os.path.realpath(root)
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(candidate)
    if not (real == root or real.startswith(root + os.sep)):
        raise SafetyViolation(f"path escapes sandbox root: {path} -> {real}")
    return real


def safe_write(path: str, content: str, root: str = ROOT) -> None:
    target = guard_path(path, root)
    with open(target, "w") as f:
        f.write(content)


def safe_read(path: str, root: str = ROOT) -> str:
    target = guard_path(path, root)
    with open(target) as f:
        return f.read()


def guard_command(argv: list) -> None:
    if not argv:
        raise SafetyViolation("empty command")
    bin0 = argv[0]
    bin_real = os.path.realpath(bin0) if os.sep in bin0 else bin0
    if bin0 not in ALLOWED_BINS and bin_real not in ALLOWED_BINS:
        raise SafetyViolation(f"blocked binary (not in allowlist): {bin0!r}")


def safe_run(argv: list, cwd: str, timeout: int = 30, root: str = ROOT):
    """Run a command as an argv list (no shell=True — no injection surface).

    cwd must resolve inside `root` (defaults to this harness's ROOT; callers
    doing hermetic third-party runs pass root=<tempdir> so the sandbox floor
    moves with the copy, not the live repo).
    """
    guard_command(argv)
    cwd_abs = guard_path(cwd, root)
    try:
        proc = subprocess.run(
            argv, cwd=cwd_abs, capture_output=True, text=True,
            timeout=timeout, shell=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"


def sha256_of_files(paths: list) -> str:
    """Combined sha256 over a list of files (order-stable). Used to prove the
    test/verification files were not touched by the agent between measurements."""
    h = hashlib.sha256()
    for p in sorted(paths):
        if os.path.exists(p):
            with open(p, "rb") as f:
                h.update(f.read())
        else:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()
