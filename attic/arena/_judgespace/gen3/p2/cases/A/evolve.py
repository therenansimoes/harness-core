"""Self-improvement loop, crossbred from gen1 v3 (mutates its own policy,
backup/rollback, real rejection recorded) and gen1 v1 (hermetic measurement
in throwaway tempdir copies, never on the live fixture).

Neither parent alone did both: v3 measured the gate against the live task
dir (not hermetic); v1's proposer had a fixed point and could never
generate a candidate worse than baseline, so its gate never rejected.

This version:
  1. Proposes a candidate mutation-search ORDER for policy.py (the thing
     harness.py actually uses to find fixes) by reversing the current
     order — a real, mechanical, non-rigged proposal.
  2. Measures BOTH baseline and candidate by copying the fixture task into
     two fresh temp directories (tempfile.mkdtemp) and running harness.run_task
     against each in isolation — no shared state, no measuring against a
     dir the candidate could have mutated.
  3. Accepts only if the candidate STRICTLY beats baseline (fewer turns,
     same or better cost) AND still solves the task. Ties or regressions
     are rejected and rolled back — `>=` is a placebo gate, so this uses `<`.
  4. Every attempt (accepted or rejected) is appended to evolve_log.jsonl.
"""
import json
import os
import shutil
import tempfile
import time

import harness
import policy
from safety import ROOT, SafetyViolation

CONFIG_PATH = os.path.join(ROOT, "config.json")
EVOLVE_LOG = os.path.join(ROOT, "evolve_log.jsonl")
FIXTURE_TASK = os.path.join(ROOT, "fixture", "task")

DEFAULT_ORDER = list(range(len(policy.MUTATIONS)))


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"mutation_order": DEFAULT_ORDER}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def propose_candidate_order(current_order):
    """Deterministic, mechanical proposal: reverse the search order.
    Sometimes this finds the fix faster, sometimes slower/never — the
    proposer has no foreknowledge of which, that's the whole point of
    measuring it instead of assuming it."""
    return list(reversed(current_order))


def metric(res):
    """Lower is better. Failure is an infinite penalty so a candidate that
    stops solving the task can never look better than one that does."""
    if not res["success"]:
        return 10_000
    return res["turns"] * 100 + res["total_cost_usd"] * 1000


def measure(order, label):
    """Hermetic run: fresh temp copy of the fixture, isolated trace file,
    never touches the live fixture/ or the other candidate's workspace."""
    tmp = tempfile.mkdtemp(prefix=f"evolve_{label}_")
    task_copy = os.path.join(tmp, "task")
    shutil.copytree(FIXTURE_TASK, task_copy)
    trace_copy = os.path.join(tmp, "trace.jsonl")
    try:
        res = harness.run_task(
            task_copy, "Make the failing tests pass.",
            max_turns=8, trace_path=trace_copy, mutation_order=order,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def evolve_once():
    cfg = load_config()
    base_order = cfg.get("mutation_order", DEFAULT_ORDER)
    candidate_order = propose_candidate_order(base_order)

    base_res = measure(base_order, "base")
    cand_res = measure(candidate_order, "cand")
    base_metric = metric(base_res)
    cand_metric = metric(cand_res)

    accepted = cand_res["success"] and cand_metric < base_metric  # strict
    if accepted:
        save_config({"mutation_order": candidate_order})

    entry = {
        "ts": time.time(),
        "base_order": base_order, "base_metric": base_metric, "base_result": base_res,
        "candidate_order": candidate_order, "candidate_metric": cand_metric, "candidate_result": cand_res,
        "accepted": accepted,
        "reason": "strict_improvement" if accepted else (
            "candidate_failed_task" if not cand_res["success"] else "no_strict_improvement_ge_baseline"
        ),
    }
    with open(EVOLVE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


if __name__ == "__main__":
    result = evolve_once()
    print(json.dumps({k: v for k, v in result.items() if k not in ("base_result", "candidate_result")}, indent=2))
    print(f"accepted={result['accepted']} reason={result['reason']}")
