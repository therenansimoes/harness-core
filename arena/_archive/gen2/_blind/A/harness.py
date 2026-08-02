"""Agent loop: task in -> act -> verify -> trace -> stop-on-green or max_turns."""
import argparse
import sys
import time
from pathlib import Path

from safety import check_path, check_command, SafetyViolation
from tracelib import Trace
from verify import run_tests
import policy


def run_task(task_dir: str, instruction: str, max_turns: int = 5, trace_path: str = None, test_cmd=None):
    task_dir = Path(task_dir).resolve()
    trace_path = Path(trace_path) if trace_path else task_dir / "trace.jsonl"
    if trace_path.exists():
        trace_path.unlink()
    tr = Trace(trace_path)

    last_output = ""
    for turn in range(1, max_turns + 1):
        t0 = time.time()
        result = run_tests(task_dir, test_cmd=test_cmd)
        tr.log_turn(turn, {"type": "verify"}, result["output_tail"], ok=result["passed"], t0=t0)
        if result["passed"]:
            tr.log_turn(turn, {"type": "stop"}, "tests green, stopping", ok=True, t0=t0)
            return {"success": True, "turns": turn, "trace": str(trace_path)}
        last_output = result["output_tail"]

        t0 = time.time()
        action = policy.propose_action(task_dir, instruction, last_output, turn)
        tokens_in = action.pop("_tokens_in", 0)
        tokens_out = action.pop("_tokens_out", 0)
        cost_usd = action.pop("_cost_usd", 0.0)

        if action["type"] == "done":
            tr.log_turn(turn, action, "policy declared done but tests still red", ok=False,
                        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, t0=t0)
            break

        if action["type"] == "shell":
            try:
                check_command(action.get("cmd", ""))
                tr.log_turn(turn, action, "shell action accepted by safety check (not executed by design: no arbitrary exec backend)",
                            ok=True, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, t0=t0)
            except SafetyViolation as e:
                tr.log_turn(turn, action, f"BLOCKED: {e}", ok=False,
                            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, t0=t0)
            continue

        if action["type"] == "edit_file":
            try:
                safe_path = check_path(action["path"], task_dir)
            except SafetyViolation as e:
                tr.log_turn(turn, action, f"BLOCKED: {e}", ok=False,
                            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, t0=t0)
                continue
            safe_path.write_text(action["content"])
            tr.log_turn(turn, action, f"wrote {safe_path}", ok=True,
                        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, t0=t0)
        else:
            tr.log_turn(turn, action, "unknown action type", ok=False,
                        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, t0=t0)

    final = run_tests(task_dir, test_cmd=test_cmd)
    tr.log_turn(max_turns + 1, {"type": "final_verify"}, final["output_tail"], ok=final["passed"])
    return {"success": final["passed"], "turns": max_turns, "trace": str(trace_path)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("--instruction", default="Make the failing tests pass.")
    ap.add_argument("--max-turns", type=int, default=5)
    args = ap.parse_args()
    res = run_task(args.task_dir, args.instruction, args.max_turns)
    print(res)
    sys.exit(0 if res["success"] else 1)
