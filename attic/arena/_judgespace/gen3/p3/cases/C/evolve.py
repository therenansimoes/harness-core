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


def propose_reverse(order, **_):
    """Reverse the whole search order."""
    return list(reversed(order))


def propose_shift(order, **_):
    """Rotate the order by half — a different mutation family goes first."""
    n = len(order)
    k = n // 2
    return order[k:] + order[:k]


def propose_swap_halves_by_stats(order, stats=None, **_):
    """Trace-driven: pull mutation indices that actually WON past rounds
    (per mutation_hits in evolve_log.jsonl) to the front, keep the rest in
    their relative order. Falls back to a plain reverse if no stats yet —
    a real bias, not a coin flip, but only once there is real evidence."""
    stats = stats or {}
    if not stats:
        return list(reversed(order))
    winners = [i for i in order if stats.get(i, 0) > 0]
    losers = [i for i in order if stats.get(i, 0) <= 0]
    return winners + losers


def crossover(order_a, order_b):
    """Ordered crossover (OX): take a contiguous slice from order_a,
    fill the remaining slots with order_b's indices in order_b's order,
    skipping anything already placed. Deterministic split at the midpoint
    so re-running the same pair reproduces the same child."""
    n = len(order_a)
    cut = n // 2
    slice_a = order_a[:cut]
    seen = set(slice_a)
    rest = [i for i in order_b if i not in seen]
    return slice_a + rest


def mutation_win_stats():
    """Read evolve_log.jsonl for which mutation index actually solved the
    task fastest across past rounds, so proposals can be steered by what
    the trace shows working instead of blind reordering."""
    stats = {}
    if not os.path.exists(EVOLVE_LOG):
        return stats
    with open(EVOLVE_LOG) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            for side in ("base_result", "candidate_result"):
                res = entry.get(side) or {}
                if not res.get("success"):
                    continue
                order = entry.get("base_order" if side == "base_result" else "candidate_order") or []
                if order:
                    stats[order[0]] = stats.get(order[0], 0) + 1
    return stats


PROPOSERS = [propose_reverse, propose_shift, propose_swap_halves_by_stats]


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
    """Widened search: generate several candidate orders from independent
    strategies (reverse, rotate, trace-stats-biased, and a crossover of the
    two best-known orders) instead of a single fixed-point proposal, then
    hermetically measure ALL of them plus the base and accept only the
    strict winner. This is the fix for the fixed-point failure mode every
    prior generation hit: a proposer with one move converges to "propose
    the same losing flip forever" once base beats it once."""
    cfg = load_config()
    base_order = cfg.get("mutation_order", DEFAULT_ORDER)
    stats = mutation_win_stats()

    candidates = {}
    for proposer in PROPOSERS:
        order = proposer(base_order, stats=stats)
        candidates[proposer.__name__] = order
    # crossover between the two structurally-different proposals so far
    names = list(candidates)
    if len(names) >= 2:
        candidates["crossover"] = crossover(candidates[names[0]], candidates[names[1]])

    base_res = measure(base_order, "base")
    base_metric = metric(base_res)

    results = {}
    for name, order in candidates.items():
        res = measure(order, name)
        results[name] = {"order": order, "result": res, "metric": metric(res)}

    best_name = min(results, key=lambda n: results[n]["metric"])
    best = results[best_name]
    accepted = best["result"]["success"] and best["metric"] < base_metric  # strict
    if accepted:
        save_config({"mutation_order": best["order"]})

    entry = {
        "ts": time.time(),
        "base_order": base_order, "base_metric": base_metric, "base_result": base_res,
        "proposals": {n: {"order": r["order"], "metric": r["metric"], "success": r["result"]["success"]}
                      for n, r in results.items()},
        "candidate_order": best["order"], "candidate_metric": best["metric"],
        "candidate_result": best["result"], "candidate_strategy": best_name,
        "accepted": accepted,
        "reason": "strict_improvement" if accepted else (
            "candidate_failed_task" if not best["result"]["success"] else "no_strict_improvement_ge_baseline"
        ),
    }
    with open(EVOLVE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


if __name__ == "__main__":
    result = evolve_once()
    print(json.dumps({k: v for k, v in result.items() if k not in ("base_result", "candidate_result")}, indent=2))
    print(f"accepted={result['accepted']} reason={result['reason']}")
