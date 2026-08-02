"""Minimal autonomous agent harness.

Loop: read task -> agent proposes a patch -> apply -> verify deterministically
-> log a trace record (tokens/cost/time/result) -> stop on pass or budget.

LLM backend: uses Anthropic API if ANTHROPIC_API_KEY is set (real network
call, real tokens). Otherwise falls back to a tiny deterministic heuristic
"stub agent" so the loop is still runnable end-to-end with zero deps/network.
This fallback is NOT an LLM — it is a rule-based patcher, labeled as such in
every trace record so results are never misrepresented as model output.
"""
import json
import os
import sys
import time

from safety import safe_read, safe_write, ROOT
from verify import verify_workspace

TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
WORKSPACE = "workspace"
TASK_FILE = os.path.join(WORKSPACE, "task.py")


def log_trace(record: dict) -> None:
    record["ts"] = time.time()
    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def call_llm_real(task_desc: str, current_code: str):
    import anthropic  # only imported if key present
    client = anthropic.Anthropic()
    t0 = time.time()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": (
                f"Task: {task_desc}\n\nCurrent file content:\n{current_code}\n\n"
                "Return ONLY the corrected full file content, no markdown fences, no prose."
            ),
        }],
    )
    elapsed = time.time() - t0
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    usage = {
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }
    return text.strip(), usage, elapsed, "anthropic:claude-haiku-4-5"


def call_llm_stub(task_desc: str, current_code: str):
    """Deterministic rule-based fallback agent (no LLM, no network).

    Heuristic: flip the most common single-char arithmetic-op bugs.
    This is intentionally dumb — it exists so the loop is demonstrable
    without an API key. Real intelligence comes from call_llm_real.
    """
    t0 = time.time()
    fixed = current_code
    for wrong, right in [(" a - b", " a + b"), (" a * b", " a + b")]:
        if wrong in fixed and "should be a + b" in fixed:
            fixed = fixed.replace(wrong, right)
    elapsed = time.time() - t0
    usage = {"input_tokens": 0, "output_tokens": 0}
    return fixed, usage, elapsed, "stub:heuristic-patcher"


def agent_step(task_desc: str, step: int) -> dict:
    current_code = safe_read(TASK_FILE)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            new_code, usage, elapsed, model = call_llm_real(task_desc, current_code)
        except Exception as e:
            new_code, usage, elapsed, model = call_llm_stub(task_desc, current_code)
            model += f" (real call failed: {e})"
    else:
        new_code, usage, elapsed, model = call_llm_stub(task_desc, current_code)

    changed = new_code != current_code
    if changed:
        safe_write(TASK_FILE, new_code)

    result = verify_workspace(WORKSPACE)

    record = {
        "step": step,
        "task": task_desc,
        "model": model,
        "usage": usage,
        "latency_sec": round(elapsed, 4),
        "patch_applied": changed,
        "verify_passed": result["passed"],
        "verify_stdout": result["stdout"][:500],
    }
    log_trace(record)
    return record


def run(task_desc: str, max_steps: int = 3):
    for step in range(1, max_steps + 1):
        record = agent_step(task_desc, step)
        print(f"[step {step}] model={record['model']} patch_applied={record['patch_applied']} "
              f"verify_passed={record['verify_passed']}")
        if record["verify_passed"]:
            print("TASK SOLVED")
            return True
    print("TASK NOT SOLVED within budget")
    return False


if __name__ == "__main__":
    task_desc = sys.argv[1] if len(sys.argv) > 1 else "Fix workspace/task.py so all tests in workspace/test_task.py pass."
    ok = run(task_desc)
    sys.exit(0 if ok else 1)
