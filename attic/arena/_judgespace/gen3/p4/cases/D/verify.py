"""Deterministic verifier — code decides pass/fail, never an LLM.

Two invariants enforced here, both called out in gen1 FEEDBACK.md as
missing everywhere:
  1. test_*.py files are hashed (SHA-256) before and after the agent's
     turn. If they changed, the turn is a hard FAIL regardless of the
     reported pytest result — the agent cannot pass by editing the exam.
  2. The verifier runs in a subprocess via safety.safe_run, so it is
     itself under the same allowlist/path-guard invariant as the agent.
"""
import hashlib
import os
import sys

from safety import safe_run, guard_path


def hash_tests(task_dir: str) -> dict:
    task_dir = guard_path(task_dir) if not os.path.isabs(task_dir) else task_dir
    hashes = {}
    for root, _dirs, files in os.walk(task_dir):
        for fn in files:
            if fn.startswith("test_") and fn.endswith(".py"):
                p = os.path.join(root, fn)
                with open(p, "rb") as f:
                    hashes[os.path.relpath(p, task_dir)] = hashlib.sha256(f.read()).hexdigest()
    return hashes


def run_tests(task_dir: str, timeout: int = 20):
    """Returns (passed: bool, output: str)."""
    binary = sys.executable
    rc, out, err = safe_run([binary, "-m", "pytest", "-q", "--tb=short"], cwd=task_dir, timeout=timeout)
    return rc == 0, (out + "\n" + err)


def verify_turn(task_dir: str, tests_before: dict):
    """Full gate for one agent turn: tests unchanged AND tests pass."""
    tests_after = hash_tests(task_dir)
    tests_tampered = tests_after != tests_before
    passed, output = run_tests(task_dir)
    ok = passed and not tests_tampered
    return {
        "tests_passed": passed,
        "tests_tampered": tests_tampered,
        "verified": ok,
        "output_tail": output[-1500:],
    }
