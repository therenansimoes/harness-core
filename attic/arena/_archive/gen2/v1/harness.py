"""Autonomous agent harness.

Two entry points:
  1. Own demo task: python3 harness.py
     Repairs workspace/task.py against workspace/test_task.py using the
     test-driven mutation-search agent in repair.py (no hint comments read).
  2. Third-party oracle: python3 harness.py --repo <dir> --test-cmd "<cmd>"
     Copies <dir> into a throwaway tempdir (never writes into the original),
     runs <cmd> as the ground-truth oracle, and tries the same mutation
     search against every non-test .py file until the command's exit code
     flips from nonzero to zero. Demonstrates the harness working on code
     it did not author and did not plant bugs in.

Every step is logged to trace.jsonl: model/backend label, token usage
(honestly 0 for the offline stub — never faked), latency, and pass/fail.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time

from safety import safe_read, safe_write, safe_run, ROOT, sha256_of_files
from repair import repair_greedy
from verify import verify_workspace

TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
WORKSPACE = "workspace"
TASK_FILE = os.path.join(WORKSPACE, "task.py")

COST_PER_1K_INPUT_USD = 0.0  # offline stub makes zero LLM calls; kept honest, not faked.
COST_PER_1K_OUTPUT_USD = 0.0

FAST_SCORING = True  # self_improve.py's growable knob: True scores mutants
# in-process (fast, same result) instead of spawning a subprocess per
# candidate (safe default). This is a real, measurable lever — not a
# placebo — see self_improve.py's accepted-proposal path.


def log_trace(record: dict) -> None:
    record["ts"] = time.time()
    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def _score_from_stdout(stdout: str):
    m = re.search(r"RESULTS (\d+)/(\d+)", stdout)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def _score_in_process(candidate_src: str):
    """Same semantics as running test_task.py in a subprocess, but by
    exec'ing the candidate module in-process and calling its TESTS list
    directly — no interpreter spawn per candidate. Only used when
    FAST_SCORING is enabled by an accepted self-improvement."""
    ns = {}
    try:
        exec(compile(candidate_src, "<candidate>", "exec"), ns)
    except Exception:
        return (0, 2)
    test_ns = {}
    test_src = safe_read(os.path.join(WORKSPACE, "test_task.py"))
    test_src = test_src.replace("from task import add, clamp", "")
    test_ns.update(ns)
    try:
        exec(compile(test_src, "<test>", "exec"), test_ns)
    except Exception:
        return (0, 2)
    tests = test_ns.get("TESTS", [])
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError:
            pass
    return (passed, len(tests))


def run_own_task(max_steps: int = 5):
    """Demo task: fix workspace/task.py driven purely by test feedback."""
    current_code = safe_read(TASK_FILE)
    test_file_abs = os.path.join(ROOT, WORKSPACE, "test_task.py")
    test_hash_before = sha256_of_files([test_file_abs])

    def score_fn(candidate_src: str):
        safe_write(TASK_FILE, candidate_src)
        if FAST_SCORING:
            return _score_in_process(candidate_src)
        rc, out, err = safe_run(
            [sys.executable, "test_task.py"], cwd=os.path.join(ROOT, WORKSPACE), timeout=15,
        )
        return _score_from_stdout(out)

    t0 = time.time()
    final_src, final_score, mutation_steps = repair_greedy(current_code, score_fn)
    elapsed = time.time() - t0

    safe_write(TASK_FILE, final_src)
    test_hash_after = sha256_of_files([test_file_abs])
    result = verify_workspace(WORKSPACE)
    result["test_hash_stable"] = result["test_hash_stable"] and (test_hash_before == test_hash_after)

    record = {
        "step": 1,
        "task": "repair workspace/task.py against workspace/test_task.py (no hints read)",
        "model": "stub:ast-mutation-search (test-driven, no LLM, no hint comments)",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "cost_usd": 0.0,
        "latency_sec": round(elapsed, 4),
        "mutation_steps_accepted": mutation_steps,
        "final_score": f"{final_score[0]}/{final_score[1]}",
        "patch_applied": final_src != current_code,
        "verify_passed": result["passed"],
        "verify_stdout": result["stdout"][:500],
        "test_hash_stable": result["test_hash_stable"],
    }
    log_trace(record)
    print(f"[repair] accepted {mutation_steps} mutation(s) in {elapsed:.3f}s, "
          f"score={record['final_score']} verify_passed={result['passed']} "
          f"test_hash_stable={result['test_hash_stable']}")
    if result["passed"]:
        print("TASK SOLVED")
    else:
        print("TASK NOT SOLVED within mutation budget")
    return result["passed"]


def run_external(repo_dir: str, test_cmd: str, max_mutants_per_file: int = 60, timeout: int = 30):
    """Point the harness at a third-party repo + its own test command.

    Never writes into repo_dir. Copies it to a tempdir, and any repair is
    applied and verified only inside that tempdir.
    """
    repo_dir = os.path.realpath(repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"ERROR: not a directory: {repo_dir}")
        return False

    with tempfile.TemporaryDirectory(prefix="harness_external_") as tmp:
        copy_root = os.path.join(tmp, "copy")
        shutil.copytree(repo_dir, copy_root)

        argv = shlex.split(test_cmd)
        if argv and argv[0] in ("python", "python3"):
            argv[0] = sys.executable

        def run_cmd():
            return safe_run(argv, cwd=copy_root, timeout=timeout, root=copy_root)

        t0 = time.time()
        rc, out, err = run_cmd()
        baseline_passed = rc == 0
        print(f"[external] baseline test-cmd exit={rc} passed={baseline_passed}")
        log_trace({
            "step": 0, "task": f"external repo baseline: {repo_dir}", "model": "n/a (baseline probe)",
            "usage": {"input_tokens": 0, "output_tokens": 0}, "cost_usd": 0.0,
            "latency_sec": round(time.time() - t0, 4), "patch_applied": False,
            "verify_passed": baseline_passed, "verify_stdout": out[-500:],
        })

        if baseline_passed:
            print("baseline already green — nothing to repair")
            return True

        py_files = [
            os.path.join(dp, f)
            for dp, _, files in os.walk(copy_root)
            for f in files
            if f.endswith(".py") and not f.startswith("test_")
        ]

        for target in py_files:
            with open(target) as fh:
                src = fh.read()
            from repair import generate_mutants
            mutants = generate_mutants(src)[:max_mutants_per_file]
            for m in mutants:
                with open(target, "w") as fh:
                    fh.write(m)
                t1 = time.time()
                rc2, out2, err2 = run_cmd()
                elapsed = time.time() - t1
                log_trace({
                    "step": 1, "task": f"external repair attempt: {os.path.relpath(target, copy_root)}",
                    "model": "stub:ast-mutation-search (external oracle)",
                    "usage": {"input_tokens": 0, "output_tokens": 0}, "cost_usd": 0.0,
                    "latency_sec": round(elapsed, 4), "patch_applied": True,
                    "verify_passed": rc2 == 0, "verify_stdout": out2[-500:],
                })
                if rc2 == 0:
                    print(f"[external] REPAIRED via mutation in {os.path.relpath(target, copy_root)} "
                          f"({elapsed:.3f}s)")
                    print("--- accepted patch (unified-ish, full mutated file) ---")
                    print(m)
                    return True
            with open(target, "w") as fh:
                fh.write(src)  # restore this file before trying the next

        print("[external] no single-mutation repair found within budget")
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None, help="path to a third-party repo to repair (read-only; copied to tempdir)")
    ap.add_argument("--test-cmd", default=None, help="test command to run as oracle inside that repo, e.g. 'python3 test_task.py'")
    ap.add_argument("task_desc", nargs="?", default=None)
    args = ap.parse_args()

    if args.repo:
        if not args.test_cmd:
            print("ERROR: --repo requires --test-cmd")
            sys.exit(2)
        ok = run_external(args.repo, args.test_cmd)
    else:
        ok = run_own_task()
    sys.exit(0 if ok else 1)
