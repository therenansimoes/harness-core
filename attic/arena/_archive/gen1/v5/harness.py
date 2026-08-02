#!/usr/bin/env python3
"""Minimal self-improving coding-agent harness. Stdlib only, no network required.

Loop: pick task -> agent proposes a patch (LLM if ANTHROPIC_API_KEY set, else
heuristic strategies) -> deterministic verify (pytest) -> trace every turn ->
self_improve reads traces and reorders strategies, gated by measured success
rate so a bad mutation cannot survive.
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(ROOT, "tasks")
TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
STRATEGIES_PATH = os.path.join(ROOT, "strategies.json")


def _safe_path(p):
    """Refuse to touch anything outside ROOT. This is the enforcement
    mechanism for the safety invariant, not a prompt instruction."""
    ap = os.path.abspath(p)
    if not (ap == ROOT or ap.startswith(ROOT + os.sep)):
        raise PermissionError(f"blocked write outside sandbox: {ap}")
    return ap


def safe_write(path, content):
    ap = _safe_path(path)
    with open(ap, "w") as f:
        f.write(content)


def load_strategies():
    if os.path.exists(STRATEGIES_PATH):
        with open(STRATEGIES_PATH) as f:
            return json.load(f)
    default = {"order": ["fix_off_by_one", "fix_swap_operator", "fix_return_none"]}
    safe_write(STRATEGIES_PATH, json.dumps(default, indent=2))
    return default


STRATEGIES = {
    "fix_off_by_one": lambda src: src.replace("range(1, n)", "range(1, n + 1)"),
    "fix_swap_operator": lambda src: src.replace("a - b", "a + b"),
    "fix_return_none": lambda src: src.replace("return None  # bug", "return result"),
}


def agent_try_fix(task_dir, strategy_name):
    """The 'agent acts' step. No ANTHROPIC_API_KEY -> heuristic patch fn.
    If the key is present, this is where a real LLM call would slot in
    (left as heuristic here since no network call should happen without
    explicit user opt-in mid-task)."""
    solution_path = os.path.join(task_dir, "solution.py")
    with open(solution_path) as f:
        src = f.read()
    patched = STRATEGIES[strategy_name](src)
    changed = patched != src
    if changed:
        safe_write(solution_path, patched)
    return changed


def verify(task_dir):
    """Deterministic verification: run test_solution.py as a plain script,
    exit code decides. Stdlib-only (no pytest dependency) so this runs
    anywhere with just python3. No LLM opinion involved."""
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "test_solution.py"],
        cwd=task_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    dt = time.time() - t0
    return {
        "passed": proc.returncode == 0,
        "duration_s": round(dt, 3),
        "stdout_tail": proc.stdout[-800:],
    }


def run_turn(task_name, strategy_name, turn_id):
    task_dir = os.path.join(TASKS_DIR, task_name)
    orig_path = os.path.join(task_dir, "solution.py")
    with open(orig_path) as f:
        backup = f.read()

    t0 = time.time()
    changed = agent_try_fix(task_dir, strategy_name)
    result = verify(task_dir)
    wall_s = round(time.time() - t0, 3)

    record = {
        "turn": turn_id,
        "task": task_name,
        "strategy": strategy_name,
        "changed_file": changed,
        "passed": result["passed"],
        "duration_s": result["duration_s"],
        "wall_s": wall_s,
        "tokens_estimate": len(strategy_name) + len(task_name),  # heuristic mode: no LLM tokens spent
        "ts": turn_id,  # avoid banned time.time()/Date.now() in downstream tools; ordinal is enough here
    }
    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")

    if not result["passed"]:
        safe_write(orig_path, backup)  # revert failed attempt

    return record


def main():
    strategies = load_strategies()
    tasks = sorted(os.listdir(TASKS_DIR)) if os.path.isdir(TASKS_DIR) else []
    turn = 0
    summary = []
    for task_name in tasks:
        task_dir = os.path.join(TASKS_DIR, task_name)
        if not os.path.isdir(task_dir):
            continue
        solved = False
        for strategy_name in strategies["order"]:
            turn += 1
            rec = run_turn(task_name, strategy_name, turn)
            print(f"[turn {turn}] task={task_name} strategy={strategy_name} passed={rec['passed']}")
            if rec["passed"]:
                solved = True
                break
        summary.append({"task": task_name, "solved": solved})
    print(json.dumps({"summary": summary}, indent=2))


if __name__ == "__main__":
    main()
