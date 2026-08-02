"""Deterministic verifier: runs a test command via subprocess and reports
pass/fail from the exit code — no LLM in the loop.

Also carries the tamper-evidence check: SHA-256 of every test_*.py (or the
custom test file, for external targets) before and after a turn. If any of
them changed, the turn is void regardless of exit code — an agent (or its
own patch search) cannot pass by rewriting the oracle.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from safety import hash_files


def _discover_test_files(task_dir: Path):
    return sorted(Path(task_dir).glob("test_*.py"))


def run_tests(task_dir, test_cmd=None, timeout=20):
    task_dir = Path(task_dir)
    test_files = _discover_test_files(task_dir)
    before = hash_files(test_files)

    # rapid rewrite-then-rerun (mutation search) can hit two writes within the
    # same filesystem mtime tick; a stale __pycache__ .pyc would then survive
    # invalidation and the subprocess would test the OLD source. Both guards
    # below close that: no bytecode written, and any cache from a previous
    # process is wiped before each run.
    pycache = task_dir / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache, ignore_errors=True)

    if test_cmd is None:
        cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(task_dir)]
    else:
        cmd = test_cmd

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            cmd, cwd=str(task_dir), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        passed = proc.returncode == 0
    except subprocess.TimeoutExpired:
        output = f"TIMEOUT after {timeout}s running {cmd}"
        passed = False

    after = hash_files(_discover_test_files(task_dir))
    if before != after:
        passed = False
        output += "\nORACLE TAMPERED: test_*.py hash changed during this turn — void."

    n_pass = output.count(" passed")
    return {"passed": passed, "output_tail": output[-2000:], "test_hashes": after}
