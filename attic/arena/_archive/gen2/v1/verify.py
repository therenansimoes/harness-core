"""Deterministic verifier. No LLM involved — exit code from a test command
decides pass/fail. Works two ways:

  1. Own demo fixture: verify_workspace() runs workspace/test_task.py.
  2. Third-party oracle: verify_external(repo_dir, test_cmd) runs an
     arbitrary test command (as argv, no shell=True) inside an arbitrary
     directory that is NOT this harness's own code — the "point the harness
     at someone else's repo" path.

Both paths hash the test file(s) before and after the run and fail the
verification if the hash changed — the acceptance criterion cannot live in
a directory the agent can quietly rewrite.
"""
import shlex
import sys

from safety import safe_run, sha256_of_files, ROOT
import os


def verify_workspace(workspace="workspace"):
    test_file = os.path.join(ROOT, workspace, "test_task.py")
    before = sha256_of_files([test_file])
    rc, out, err = safe_run(
        [sys.executable, "test_task.py"],
        cwd=os.path.join(ROOT, workspace), timeout=30,
    )
    after = sha256_of_files([test_file])
    return {
        "passed": rc == 0 and before == after,
        "returncode": rc,
        "stdout": out[-4000:],
        "stderr": err[-2000:],
        "test_hash_stable": before == after,
    }


def verify_external(repo_dir: str, test_cmd: str, test_glob_hint: list = None, timeout: int = 60):
    """Run a third-party test command as the oracle. repo_dir is treated as
    its own sandbox root for this call (safe_run's cwd guard is anchored to
    it), so the harness's own ROOT is never touched by this path.
    """
    repo_dir = os.path.realpath(repo_dir)
    argv = shlex.split(test_cmd)
    # normalize a bare "pytest"/"python" invocation to the running interpreter
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable

    test_files = test_glob_hint or []
    before = sha256_of_files(test_files) if test_files else None

    rc, out, err = safe_run(argv, cwd=repo_dir, timeout=timeout, root=repo_dir)

    after = sha256_of_files(test_files) if test_files else None
    stable = True if before is None else (before == after)
    return {
        "passed": rc == 0 and stable,
        "returncode": rc,
        "stdout": out[-4000:],
        "stderr": err[-2000:],
        "test_hash_stable": stable,
    }


if __name__ == "__main__":
    r = verify_workspace()
    print("PASS" if r["passed"] else "FAIL")
    print(r["stdout"])
    sys.exit(0 if r["passed"] else 1)
