"""Deterministic verifier. No LLM involved — exit code from pytest decides."""
import sys
from safety import safe_run, ROOT
import os


def verify_workspace(workspace="workspace"):
    rc, out, err = safe_run(
        [sys.executable, "test_task.py"],
        cwd=os.path.join(ROOT, workspace), timeout=30,
    )
    return {
        "passed": rc == 0,
        "returncode": rc,
        "stdout": out[-4000:],
        "stderr": err[-2000:],
    }


if __name__ == "__main__":
    r = verify_workspace()
    print("PASS" if r["passed"] else "FAIL")
    print(r["stdout"])
    sys.exit(0 if r["passed"] else 1)
