"""Agent loop: automated program repair via mutation search.

No LLM, no network -- this is the harness's offline policy, guaranteed to
run without an API key (see run.sh). The Policy interface below is what an
LLM-backed policy would slot into instead; propose_candidates() is the only
method a different policy needs to implement.

Technique: classic mutation-testing operators (comparison-operator flip,
arithmetic-operator flip, boundary-constant flip) run in reverse -- instead
of injecting bugs to test a suite, we search the mutation space for the
patch that makes a RED suite go GREEN. Each candidate is independently
gated by verifier.py (real subprocess + real pytest, sandboxed), so nothing
here is aware of what the "correct" answer is.
"""
import ast
import copy
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import verifier  # noqa: E402

HARNESS_DIR = os.path.dirname(__file__)
PKG_FILE = os.path.join(HARNESS_DIR, "fixture", "pkg", "calc.py")
POLICY_FILE = os.path.join(HARNESS_DIR, "policy.json")
TRACE_FILE = os.path.join(HARNESS_DIR, "trace.jsonl")

CMP_FLIPS = {ast.Gt: ast.Lt, ast.Lt: ast.Gt, ast.GtE: ast.LtE, ast.LtE: ast.GtE}
ARITH_FLIPS = {ast.Add: ast.Sub, ast.Sub: ast.Add}


def load_policy():
    with open(POLICY_FILE) as f:
        return json.load(f)


def _candidates_cmp_flip(tree):
    out = []
    for i, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op_type = type(node.ops[0])
            if op_type in CMP_FLIPS:
                t2 = copy.deepcopy(tree)
                target = list(ast.walk(t2))[i]
                target.ops = [CMP_FLIPS[op_type]()]
                out.append(("cmp_flip#%d" % i, t2))
    return out


def _candidates_arith_flip(tree):
    out = []
    for i, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type in ARITH_FLIPS:
                t2 = copy.deepcopy(tree)
                target = list(ast.walk(t2))[i]
                target.op = ARITH_FLIPS[op_type]()
                out.append(("arith_flip#%d" % i, t2))
    return out


def _candidates_const_flip(tree):
    out = []
    for i, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Constant) and node.value in (0, 1):
            t2 = copy.deepcopy(tree)
            target = list(ast.walk(t2))[i]
            target.value = 1 - node.value
            out.append(("const_flip#%d(%s->%s)" % (i, node.value, 1 - node.value), t2))
    return out


GENERATORS = {
    "cmp_flip": _candidates_cmp_flip,
    "arith_op_flip": _candidates_arith_flip,
    "const_flip": _candidates_const_flip,
}


def log_trace(entry):
    entry["t_wall"] = round(time.time(), 3)
    entry["cost_usd"] = 0.0  # offline policy: zero LLM tokens, zero spend
    entry["tokens"] = 0
    with open(TRACE_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_repair(policy=None, max_candidates=200):
    policy = policy or load_policy()
    t_start = time.time()
    best_source = open(PKG_FILE).read()
    hash_before_oracle = verifier.test_oracle_hash()

    baseline = verifier.verify_turn(hash_before_oracle)
    best_passed = baseline["passed"]
    log_trace({
        "turn": 0, "action": "baseline", "mutation": None,
        "passed": baseline["passed"], "failed": baseline["failed"],
        "decision": "n/a", "oracle_intact": baseline["oracle_intact"],
    })

    turn = 0
    tried = 0
    seen = set()
    for kind in policy["mutation_order"]:
        gen = GENERATORS[kind]
        progressed = True
        while progressed and tried < max_candidates:
            progressed = False
            tree = ast.parse(best_source)
            candidates = gen(tree)
            for label, mutated_tree in candidates:
                if tried >= max_candidates:
                    break
                candidate_source = ast.unparse(ast.fix_missing_locations(mutated_tree))
                if candidate_source in seen:
                    continue
                seen.add(candidate_source)
                tried += 1
                turn += 1
                with open(PKG_FILE, "w") as f:
                    f.write(candidate_source)

                result = verifier.verify_turn(hash_before_oracle)
                strictly_better = (
                    result["oracle_intact"]
                    and result["passed"] > best_passed
                )
                decision = "accepted" if strictly_better else "rejected"
                log_trace({
                    "turn": turn, "action": "candidate", "mutation": "%s:%s" % (kind, label),
                    "passed": result["passed"], "failed": result["failed"],
                    "decision": decision, "oracle_intact": result["oracle_intact"],
                    "elapsed_s": round(time.time() - t_start, 3),
                })

                if strictly_better:
                    best_passed = result["passed"]
                    best_source = candidate_source
                    progressed = True
                    if result["ok"]:
                        with open(PKG_FILE, "w") as f:
                            f.write(best_source)
                        return {
                            "solved": True, "turns": turn, "tried": tried,
                            "final_passed": best_passed,
                            "elapsed_s": round(time.time() - t_start, 3),
                        }
                    break  # restart this kind's search from the new best_source
                else:
                    # rollback: restore the last accepted-good source
                    with open(PKG_FILE, "w") as f:
                        f.write(best_source)

    # exhausted search space -- leave the best-known (possibly non-passing) state
    with open(PKG_FILE, "w") as f:
        f.write(best_source)
    return {
        "solved": best_passed == baseline["total"] and best_passed > 0,
        "turns": turn, "tried": tried, "final_passed": best_passed,
        "elapsed_s": round(time.time() - t_start, 3),
    }


if __name__ == "__main__":
    print(json.dumps(run_repair(), indent=2))
