"""Self-improvement loop: the harness reads its OWN trace, proposes a change
to its OWN config (policy.json, which agent.py's mutation search reads),
and a gate accepts or rejects that change against a strict, measurable
criterion -- fewer candidates tried to reach a fully-green fixture, on the
same task, from the same reset state.

This is deliberately separate from the per-candidate gate inside agent.py:
that one gates *task* patches; this one gates *harness* self-modification.
"""
import json
import os
import shutil

import agent
import fixture_seed

HARNESS_DIR = os.path.dirname(__file__)
POLICY_FILE = os.path.join(HARNESS_DIR, "policy.json")
SELF_TRACE_FILE = os.path.join(HARNESS_DIR, "self_improve_trace.jsonl")


def _order_by_trace_effectiveness(trace_path, fallback_order):
    """Rank mutation kinds by how many accepted candidates they produced in
    the most recent run -- kinds that pay off more should be tried first."""
    score = {}
    if os.path.exists(trace_path):
        for line in open(trace_path):
            d = json.loads(line)
            if d.get("action") != "candidate":
                continue
            kind = d["mutation"].split(":")[0]
            score.setdefault(kind, 0)
            if d["decision"] == "accepted":
                score[kind] += 1
    ranked = sorted(fallback_order, key=lambda k: -score.get(k, 0))
    return ranked


def log_self(entry):
    with open(SELF_TRACE_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_self_improvement_cycle():
    baseline_policy = json.load(open(POLICY_FILE))

    # 1. measure current policy from a clean reset
    fixture_seed.reset()
    if os.path.exists(agent.TRACE_FILE):
        os.remove(agent.TRACE_FILE)
    baseline_result = agent.run_repair(policy=baseline_policy)
    baseline_trace_copy = agent.TRACE_FILE + ".baseline"
    shutil.copy(agent.TRACE_FILE, baseline_trace_copy)

    # 2. propose a change: reorder mutation_order by observed effectiveness
    proposed_order = _order_by_trace_effectiveness(
        baseline_trace_copy, baseline_policy["mutation_order"])
    candidate_policy = {"mutation_order": proposed_order}

    # 3. measure candidate from an identical clean reset
    fixture_seed.reset()
    os.remove(agent.TRACE_FILE)
    candidate_result = agent.run_repair(policy=candidate_policy)

    # 4. gate: strict improvement in attempts required, AND must still solve
    strictly_better = (
        candidate_result["solved"]
        and candidate_result["tried"] < baseline_result["tried"]
    )
    decision = "accepted" if strictly_better else "rejected"

    entry = {
        "baseline_policy": baseline_policy,
        "candidate_policy": candidate_policy,
        "baseline_tried": baseline_result["tried"],
        "candidate_tried": candidate_result["tried"],
        "baseline_solved": baseline_result["solved"],
        "candidate_solved": candidate_result["solved"],
        "decision": decision,
    }
    log_self(entry)

    if strictly_better:
        with open(POLICY_FILE, "w") as f:
            json.dump(candidate_policy, f, indent=2)
    else:
        # rollback: policy.json is left untouched (never written with the
        # rejected candidate), and the fixture is reset one more time so the
        # gate's own experiment leaves no residue behind.
        fixture_seed.reset()
        agent.run_repair(policy=baseline_policy)

    os.remove(baseline_trace_copy)
    return entry


if __name__ == "__main__":
    print(json.dumps(run_self_improvement_cycle(), indent=2))
