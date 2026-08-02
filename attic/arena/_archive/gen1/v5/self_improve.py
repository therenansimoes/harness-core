#!/usr/bin/env python3
"""Self-improvement loop: read trace.jsonl, propose a reordering of
strategies.json (put strategies that historically win first-try higher),
then GATE the change: re-run the full task suite with the proposed order
and only keep it if the total turns-to-solve-all improves. Deterministic
accept/reject, no LLM judgment involved.
"""
import copy
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
STRATEGIES_PATH = os.path.join(ROOT, "strategies.json")


def read_trace():
    if not os.path.exists(TRACE_PATH):
        return []
    with open(TRACE_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def propose_order(trace, current_order):
    """Rank strategies by how often they were the one that made a task
    pass, most-successful first. Ties broken by current order (stable)."""
    wins = {s: 0 for s in current_order}
    for rec in trace:
        if rec.get("passed"):
            wins[rec["strategy"]] = wins.get(rec["strategy"], 0) + 1
    return sorted(current_order, key=lambda s: -wins.get(s, 0))


def total_turns_to_solve_all():
    """Run the harness fresh and return total turns spent. Lower is better."""
    proc = subprocess.run(
        [sys.executable, "harness.py"], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    turns = [l for l in proc.stdout.splitlines() if l.startswith("[turn")]
    all_solved = '"solved": false' not in proc.stdout
    return len(turns), all_solved


def main():
    with open(STRATEGIES_PATH) as f:
        current = json.load(f)
    trace = read_trace()
    if not trace:
        print("no trace yet, nothing to learn from")
        return

    baseline_turns, baseline_ok = total_turns_to_solve_all()

    proposed_order = propose_order(trace, current["order"])
    if proposed_order == current["order"]:
        print(f"no change proposed (baseline: {baseline_turns} turns, all_solved={baseline_ok})")
        return

    candidate = copy.deepcopy(current)
    candidate["order"] = proposed_order
    backup = json.dumps(current, indent=2)
    with open(STRATEGIES_PATH, "w") as f:
        json.dump(candidate, f, indent=2)

    candidate_turns, candidate_ok = total_turns_to_solve_all()

    # Gate: reject if it solves fewer tasks, or takes >= as many turns.
    accept = candidate_ok and (candidate_turns < baseline_turns)
    if accept:
        print(f"ACCEPTED new order {proposed_order}: {baseline_turns} -> {candidate_turns} turns")
    else:
        with open(STRATEGIES_PATH, "w") as f:
            f.write(backup)
        print(
            f"REJECTED new order {proposed_order}: baseline={baseline_turns}/{baseline_ok} "
            f"candidate={candidate_turns}/{candidate_ok} (no improvement or regression)"
        )


if __name__ == "__main__":
    main()
