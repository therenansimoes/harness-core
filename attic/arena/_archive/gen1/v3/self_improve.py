"""Self-improvement loop: read trace.jsonl, propose a change to harness.py
itself, and only accept it if a deterministic gate passes.

Gate = re-run the demo task end-to-end after the change. If it still solves
the task (verify_workspace passes) AND the file still imports cleanly, the
change is accepted (kept). Otherwise it is rolled back automatically.

This is intentionally a narrow, safe demo of the mechanism: the "proposal"
is a small deterministic mutation (raise max_steps, or add a stub rule),
not a free-form LLM rewrite — because unattended free-form self-rewrite of
the harness with no human review is a correctness/safety risk on a 5-minute
budget. See NOTES.md for what a real version needs.
"""
import json
import os
import shutil
import subprocess
import sys
import time

from safety import ROOT, guard_path

HARNESS_PATH = os.path.join(ROOT, "harness.py")
TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
BACKUP_PATH = os.path.join(ROOT, "harness.py.bak")


def read_trace():
    if not os.path.exists(TRACE_PATH):
        return []
    with open(TRACE_PATH) as f:
        return [json.loads(l) for l in f if l.strip()]


def propose_change(trace: list) -> str | None:
    """Deterministic proposal policy driven by trace stats.

    If recent runs needed >1 step to solve, propose raising max_steps.
    If everything solved in step 1 already, propose nothing (no-op gate).
    """
    if not trace:
        return None
    last_solved_steps = [r["step"] for r in trace if r["verify_passed"]]
    if not last_solved_steps:
        return "increase_max_steps"
    if max(last_solved_steps) <= 1:
        return None
    return "increase_max_steps"


def apply_change(change: str) -> bool:
    shutil.copy(HARNESS_PATH, BACKUP_PATH)
    with open(HARNESS_PATH) as f:
        src = f.read()
    if change == "increase_max_steps":
        new_src = src.replace("max_steps: int = 3", "max_steps: int = 5")
        if new_src == src:
            return False
        with open(HARNESS_PATH, "w") as f:
            f.write(new_src)
        return True
    return False


def rollback():
    if os.path.exists(BACKUP_PATH):
        shutil.move(BACKUP_PATH, HARNESS_PATH)


def gate() -> bool:
    """Deterministic acceptance gate: harness must still import and still
    solve the demo task via a fresh subprocess run."""
    proc = subprocess.run(
        [sys.executable, "-c", "import harness"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        return False
    proc2 = subprocess.run(
        [sys.executable, "harness.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    return proc2.returncode == 0


def run_self_improve():
    trace = read_trace()
    change = propose_change(trace)
    if change is None:
        print("no change proposed (nothing to improve per current policy)")
        return {"proposed": None, "accepted": False}

    print(f"proposing change: {change}")
    applied = apply_change(change)
    if not applied:
        print("change was a no-op, skipping")
        return {"proposed": change, "accepted": False, "reason": "no-op"}

    ok = gate()
    if ok:
        if os.path.exists(BACKUP_PATH):
            os.remove(BACKUP_PATH)
        print("GATE PASSED: change accepted and kept")
        return {"proposed": change, "accepted": True}
    else:
        rollback()
        print("GATE FAILED: change rejected, harness.py rolled back")
        return {"proposed": change, "accepted": False, "reason": "gate_failed"}


if __name__ == "__main__":
    result = run_self_improve()
    with open(os.path.join(ROOT, "self_improve_log.jsonl"), "a") as f:
        result["ts"] = time.time()
        f.write(json.dumps(result) + "\n")
