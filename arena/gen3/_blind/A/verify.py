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


def _is_test_file(fn: str) -> bool:
    # pytest convention plus common third-party conventions (JS/Go/etc) so
    # the tamper gate isn't blind the moment --repo points outside python.
    return (
        (fn.startswith("test_") and fn.endswith(".py"))
        or fn.endswith("_test.py")
        or fn.endswith(".test.js") or fn.endswith(".spec.js")
        or fn.endswith(".test.ts") or fn.endswith(".spec.ts")
        or fn.endswith("_test.go")
    )


def hash_tests(task_dir: str) -> dict:
    task_dir = guard_path(task_dir) if not os.path.isabs(task_dir) else task_dir
    hashes = {}
    for root, dirs, files in os.walk(task_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for fn in files:
            if _is_test_file(fn):
                p = os.path.join(root, fn)
                with open(p, "rb") as f:
                    hashes[os.path.relpath(p, task_dir)] = hashlib.sha256(f.read()).hexdigest()
    return hashes


def run_tests(task_dir: str, timeout: int = 20, test_cmd: list = None):
    """Returns (passed: bool, output: str).

    test_cmd lets a third-party repo supply its OWN oracle (e.g. `npm test`,
    `go test ./...`, `python3 -m unittest`) instead of assuming pytest.
    Still routed through safe_run so the allowlist/path-guard applies to
    someone else's test command too.
    """
    binary = sys.executable
    cmd = test_cmd or [binary, "-m", "pytest", "-q", "--tb=short"]
    rc, out, err = safe_run(cmd, cwd=task_dir, timeout=timeout)
    return rc == 0, (out + "\n" + err)


def verify_turn(task_dir: str, tests_before: dict, test_cmd: list = None):
    """Full gate for one agent turn: tests unchanged AND tests pass."""
    tests_after = hash_tests(task_dir)
    tests_tampered = tests_after != tests_before
    passed, output = run_tests(task_dir, test_cmd=test_cmd)
    ok = passed and not tests_tampered
    return {
        "tests_passed": passed,
        "tests_tampered": tests_tampered,
        "verified": ok,
        "output_tail": output[-1500:],
    }
