"""Agent loop: point it at ANY task_dir with a pytest suite (yours or a
third party's), it edits files until tests pass or max_turns is hit.
Every turn is measured and appended to trace.jsonl: what happened, cost,
tokens, wall time. Deterministic verify.py decides pass/fail — never the
policy/LLM itself.
"""
import argparse
import json
import os
import time
from pathlib import Path

from safety import guard_path, safe_write, SafetyViolation
import policy
import verify

# offline backend: no tokens spent, cost is genuinely 0.0, not a fake number.
# anthropic backend: priced at published Sonnet rates (USD / token).
PRICE_IN_PER_TOK = 3.0 / 1_000_000
PRICE_OUT_PER_TOK = 15.0 / 1_000_000


def run_task(task_dir: str, instruction: str, max_turns: int = 6, trace_path: str = None,
             mutation_order: list = None, append_trace: bool = True, test_cmd: list = None):
    task_dir = os.path.realpath(task_dir)
    trace_path = trace_path or os.path.join(os.path.dirname(__file__), "trace.jsonl")
    tests_before = verify.hash_tests(task_dir)
    tried = set()
    last_output = ""
    turns_log = []
    total_cost = 0.0
    total_tokens = 0

    for turn in range(1, max_turns + 1):
        t0 = time.time()
        action = policy.propose_action(task_dir, instruction, last_output, turn, tried=tried, order=mutation_order)

        cost = 0.0
        tokens = 0
        if action.get("type") == "edit_file":
            tried.add(action.get("candidate_id"))
            target = os.path.join(task_dir, action["path"])
            # guard_path is ROOT-scoped for the harness's own files; for
            # external task dirs we still refuse traversal out of task_dir.
            real_target = os.path.realpath(target)
            if not real_target.startswith(task_dir + os.sep) and real_target != task_dir:
                raise SafetyViolation(f"agent tried to write outside task_dir: {action['path']}")
            with open(real_target, "w") as f:
                f.write(action["content"])
            if policy.BACKEND == "anthropic":
                tokens = len(action["content"]) // 4
                cost = tokens * PRICE_OUT_PER_TOK

        result = verify.verify_turn(task_dir, tests_before, test_cmd=test_cmd)
        last_output = result["output_tail"]
        dt = time.time() - t0
        total_cost += cost
        total_tokens += tokens

        entry = {
            "turn": turn,
            "action": {k: v for k, v in action.items() if k != "candidate_id"},
            "verified": result["verified"],
            "tests_passed": result["tests_passed"],
            "tests_tampered": result["tests_tampered"],
            "wall_time_s": round(dt, 4),
            "tokens": tokens,
            "cost_usd": round(cost, 6),
            "backend": policy.BACKEND,
        }
        turns_log.append(entry)
        if append_trace:
            with open(trace_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

        if result["verified"]:
            break
        if action.get("type") == "done":
            break

    success = turns_log[-1]["verified"] if turns_log else False
    return {
        "success": success,
        "turns": len(turns_log),
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": total_tokens,
        "trace": trace_path,
        "task_dir": task_dir,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir", nargs="?",
                     help="legacy positional form (kept for backward compat)")
    ap.add_argument("--repo", help="path to a THIRD-PARTY project; its own --test-cmd is the oracle")
    ap.add_argument("--test-cmd", help='shell string, e.g. "python3 -m unittest" or "npm test"')
    ap.add_argument("--instruction", default="Make the failing tests pass.")
    ap.add_argument("--max-turns", type=int, default=6)
    args = ap.parse_args()

    target_dir = args.repo or args.task_dir
    if not target_dir:
        ap.error("need a task_dir or --repo <dir>")
    import shlex
    test_cmd = shlex.split(args.test_cmd) if args.test_cmd else None

    res = run_task(target_dir, args.instruction, max_turns=args.max_turns, test_cmd=test_cmd)
    print(json.dumps(res, indent=2))
    raise SystemExit(0 if res["success"] else 1)
