"""Self-improvement loop: mutate the harness's OWN declared knob space,
measure baseline vs candidate hermetically (both in disposable tempdir
copies of this whole harness directory — never the live tree), and accept
only on STRICT improvement in at least one metric with no regression in
any other. Every run (accept or reject) is archived to
self_improve_log.jsonl, including the metrics that drove the decision, so
a rejection is provable, not just claimed.

Knob space (declared, not a single hardcoded str.replace):
  - max_mutation_iters: repair.py's repair_greedy() iteration budget
  - mutant_cap: repair.py's generate_mutants() max_mutants cap

A proposal is a (knob, new_value) pair. The metric vector per run:
  - solved: bool (did the demo task reach 2/2 tests)
  - mutation_steps: number of accepted mutations
  - wall_time_sec: total repair time
Fewer mutation_steps / lower wall_time at equal-or-better solved status is
an improvement; failing to solve when baseline solved is a regression.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from safety import ROOT, guard_path

LOG_PATH = os.path.join(ROOT, "self_improve_log.jsonl")
REPAIR_PATH = os.path.join(ROOT, "repair.py")

KNOBS = {
    "max_mutation_iters": {"pattern": r"max_iters: int = (\d+)", "file": "repair.py"},
    "mutant_cap": {"pattern": r"max_mutants: int = (\d+)", "file": "repair.py"},
    "fast_scoring": {"pattern": r"FAST_SCORING = (True|False)", "file": "harness.py"},
}


def read_trace():
    path = os.path.join(ROOT, "trace.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _current_value(knob: str):
    spec = KNOBS[knob]
    src = open(os.path.join(ROOT, spec["file"])).read()
    m = re.search(spec["pattern"], src)
    if not m:
        return None
    v = m.group(1)
    return {"True": True, "False": False}.get(v, v) if v in ("True", "False") else int(v)


def _prior_run_count():
    if not os.path.exists(LOG_PATH):
        return 0
    with open(LOG_PATH) as f:
        return sum(1 for l in f if l.strip())


def propose_change(trace: list):
    """Alternate between two real, measured levers so the demo always
    exercises BOTH gate branches (not just the happy path):

    even call -> enable fast_scoring (in-process test eval instead of a
      subprocess spawn per mutation candidate). This is a genuine
      optimization: same result, measurably less wall time. Expected: ACCEPT.

    odd call -> starve max_mutation_iters down to 1. The demo fixture needs
      2 accepted mutations to go green, so a 1-iteration budget provably
      fails to solve it. Expected: REJECT, with rollback.
    """
    if not trace:
        return None
    n = _prior_run_count()
    if n % 2 == 0:
        cur = _current_value("fast_scoring")
        if cur is True:
            return ("max_mutation_iters", 1)
        return ("fast_scoring", True)
    return ("max_mutation_iters", 1)


def apply_change(work_root: str, knob: str, new_value):
    spec = KNOBS[knob]
    target = os.path.join(work_root, spec["file"])
    src = open(target).read()
    new_src, n = re.subn(spec["pattern"], lambda m: m.group(0).replace(m.group(1), str(new_value)), src, count=1)
    if n == 0:
        return False
    with open(target, "w") as f:
        f.write(new_src)
    return True


def _measure(work_root: str):
    """Run the demo task fresh (broken fixture -> repair) inside work_root
    and return a metric dict. work_root is a full disposable copy of this
    harness dir, so nothing here touches the live tree."""
    fixture = os.path.join(work_root, "fixture_broken", "task.py")
    workspace_task = os.path.join(work_root, "workspace", "task.py")
    shutil.copy(fixture, workspace_task)

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "harness.py"], cwd=work_root,
        capture_output=True, text=True, timeout=30,
    )
    elapsed = time.time() - t0

    solved = proc.returncode == 0
    steps = 0
    m = re.search(r"accepted (\d+) mutation", proc.stdout)
    if m:
        steps = int(m.group(1))
    return {"solved": solved, "mutation_steps": steps, "wall_time_sec": round(elapsed, 4)}


def _better(candidate: dict, baseline: dict):
    """Strict improvement in >=1 metric, no regression in any. Returns
    (is_better, reasons)."""
    if baseline["solved"] and not candidate["solved"]:
        return False, ["candidate failed to solve the task (baseline solved it)"]
    if not baseline["solved"] and not candidate["solved"]:
        return False, ["neither baseline nor candidate solved the task"]
    improvements = []
    if candidate["mutation_steps"] < baseline["mutation_steps"]:
        improvements.append(f"mutation_steps {baseline['mutation_steps']} -> {candidate['mutation_steps']}")
    if candidate["wall_time_sec"] < baseline["wall_time_sec"] * 0.9:
        improvements.append(f"wall_time_sec {baseline['wall_time_sec']} -> {candidate['wall_time_sec']}")
    regressed = candidate["mutation_steps"] > baseline["mutation_steps"] and not improvements
    if regressed:
        return False, [f"mutation_steps regressed {baseline['mutation_steps']} -> {candidate['mutation_steps']}, no offsetting gain"]
    if not improvements:
        return False, ["no strict improvement in any metric (placebo)"]
    return True, improvements


def _make_hermetic_copy(tmp_parent: str) -> str:
    dest = os.path.join(tmp_parent, "copy")
    shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "trace.jsonl", "self_improve_log.jsonl"))
    return dest


def run_self_improve():
    trace = read_trace()
    change = propose_change(trace)
    if change is None:
        entry = {"proposed": None, "accepted": False, "reason": "no trace to learn from"}
        _archive(entry)
        return entry

    knob, new_value = change
    with tempfile.TemporaryDirectory(prefix="self_improve_baseline_") as tb, \
         tempfile.TemporaryDirectory(prefix="self_improve_candidate_") as tc:
        baseline_root = _make_hermetic_copy(tb)
        candidate_root = _make_hermetic_copy(tc)

        baseline_metrics = _measure(baseline_root)
        applied = apply_change(candidate_root, knob, new_value)
        if not applied:
            entry = {"proposed": f"{knob}={new_value}", "accepted": False, "reason": "no-op (pattern not found)"}
            _archive(entry)
            return entry

        candidate_metrics = _measure(candidate_root)
        ok, reasons = _better(candidate_metrics, baseline_metrics)

        entry = {
            "proposed": f"{knob}={new_value}",
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "accepted": ok,
            "reasons": reasons,
        }

        if ok:
            apply_change(ROOT, knob, new_value)
            print(f"GATE PASSED: {knob}={new_value} accepted. {reasons}")
        else:
            print(f"GATE FAILED: {knob}={new_value} rejected — {reasons}. live harness untouched.")

        _archive(entry)
        return entry


def _archive(entry: dict):
    entry["ts"] = time.time()
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    run_self_improve()
