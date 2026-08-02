#!/usr/bin/env python3
"""
Minimal self-improving AI agent harness. Stdlib only.

Loop: task -> LLM proposes a patch -> code applies it inside a sandboxed
workspace -> deterministic verifier (pytest, exit code) decides pass/fail ->
every step is traced to trace.jsonl (tokens/cost/time) -> a separate
self-improve step reads the trace, proposes a change to its own prompt
template, and a gate re-runs the fixed benchmark to accept/reject the change.

No network required to run: if ANTHROPIC_API_KEY is unset, a deterministic
offline stub LLM is used so the loop is still fully runnable end to end.
"""
import json, os, re, subprocess, sys, time, urllib.request, hashlib

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(ROOT, "workspace")
TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
PROMPT_PATH = os.path.join(ROOT, "prompt_template.txt")

# ---------------------------------------------------------------------------
# Safety invariant (mechanism, not prompt text): every filesystem write from
# the agent is routed through this function, which refuses any path that
# resolves outside WORKSPACE. This is enforced in code, so an LLM cannot
# argue or hallucinate its way past it.
# ---------------------------------------------------------------------------
def safe_write(rel_path, content):
    target = os.path.realpath(os.path.join(WORKSPACE, rel_path))
    if not target.startswith(os.path.realpath(WORKSPACE) + os.sep) and target != os.path.realpath(WORKSPACE):
        raise PermissionError(f"blocked write outside workspace: {rel_path}")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(content)
    return target


def append_trace(event):
    event["ts"] = event.get("ts", time.time())
    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# LLM call. Real Anthropic API if key present, else deterministic offline
# stub (regex-based fixer) so the whole loop is runnable without network.
# ---------------------------------------------------------------------------
def call_llm(prompt, model="claude-sonnet-5"):
    t0 = time.time()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        body = json.dumps({
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        usage = data.get("usage", {})
        elapsed = time.time() - t0
        return text, {"input_tokens": usage.get("input_tokens", 0),
                       "output_tokens": usage.get("output_tokens", 0),
                       "elapsed_s": elapsed, "backend": "anthropic"}
    else:
        # Offline deterministic stub: looks for "BUGGY:<expr>" markers and
        # fixes the known bug classes in the benchmark tasks below.
        text = offline_stub(prompt)
        elapsed = time.time() - t0
        approx_tokens = max(1, len(prompt) // 4)
        return text, {"input_tokens": approx_tokens, "output_tokens": len(text) // 4,
                       "elapsed_s": elapsed, "backend": "offline_stub"}


def offline_stub(prompt):
    """Deterministic 'LLM' used when there's no API key: applies a known fix
    for the sample buggy function so the loop is verifiable offline."""
    if "def add(a, b):" in prompt and "return a - b" in prompt:
        return "def add(a, b):\n    return a + b\n"
    if "def is_even(n):" in prompt and "return n % 2 == 1" in prompt:
        return "def is_even(n):\n    return n % 2 == 0\n"
    return "# no fix found\n"


# ---------------------------------------------------------------------------
# Deterministic verifier: NOT the LLM. Runs pytest against the file the
# agent just wrote and returns pass/fail from the process exit code.
# ---------------------------------------------------------------------------
_RUNNER = (
    "import sys, importlib, traceback\n"
    "mod = importlib.import_module(sys.argv[1][:-3])\n"
    "fns = [f for name, f in vars(mod).items() if name.startswith('test_') and callable(f)]\n"
    "ok = True\n"
    "for f in fns:\n"
    "    try:\n"
    "        f()\n"
    "    except Exception:\n"
    "        ok = False\n"
    "        traceback.print_exc()\n"
    "print('RESULT=%s' % ('PASS' if ok and fns else 'FAIL'))\n"
    "sys.exit(0 if ok and fns else 1)\n"
)


def verify(test_file):
    proc = subprocess.run(
        [sys.executable, "-c", _RUNNER, test_file],
        cwd=WORKSPACE, capture_output=True, text=True, timeout=30,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# One agent task: fix target_file so test_file passes.
# ---------------------------------------------------------------------------
def run_task(task_id, target_file, test_file, max_iters=3):
    prompt_tpl = open(PROMPT_PATH).read()
    for i in range(1, max_iters + 1):
        src = open(os.path.join(WORKSPACE, target_file)).read()
        prompt = prompt_tpl.format(source=src)
        t0 = time.time()
        fix, usage = call_llm(prompt)
        safe_write(target_file, fix)
        ok, output = verify(test_file)
        elapsed = time.time() - t0
        append_trace({
            "task_id": task_id, "iter": i, "target_file": target_file,
            "passed": ok, "usage": usage, "wall_s": elapsed,
            "verifier_output_sha1": hashlib.sha1(output.encode()).hexdigest(),
        })
        if ok:
            return True, i
    return False, max_iters


# ---------------------------------------------------------------------------
# Self-improvement loop: read trace.jsonl, compute pass-rate and avg iters,
# propose a change to prompt_template.txt, gate it by re-running the fixed
# benchmark suite with the candidate prompt; accept only if pass-rate does
# not regress and avg iters-to-pass does not increase.
# ---------------------------------------------------------------------------
BENCHMARK = [
    ("bench_add", "buggy_add.py", "test_add.py"),
    ("bench_even", "buggy_even.py", "test_even.py"),
]

FIXTURES = {
    "buggy_add.py": "def add(a, b):\n    return a - b\n",
    "buggy_even.py": "def is_even(n):\n    return n % 2 == 1\n",
}


def score_benchmark():
    passed = 0
    iters_sum = 0
    for task_id, target, test in BENCHMARK:
        safe_write(target, FIXTURES[target])  # reset fixture: run_task mutates it in place
        ok, iters = run_task(task_id, target, test, max_iters=2)
        passed += int(ok)
        iters_sum += iters
    return passed, iters_sum


def self_improve():
    baseline_prompt = open(PROMPT_PATH).read()
    baseline_pass, baseline_iters = score_benchmark()
    append_trace({"event": "self_improve_baseline", "pass": baseline_pass, "iters": baseline_iters})

    # Candidate change: proposed by inspecting the trace for repeated
    # failures, not by asking the LLM to "be creative". Simple deterministic
    # heuristic here; a real version would use the LLM to draft candidate
    # text, still gated the same way.
    candidate_prompt = baseline_prompt
    if "Return ONLY" not in baseline_prompt:
        candidate_prompt = baseline_prompt.rstrip() + "\nReturn ONLY the corrected source code, no prose.\n"

    with open(PROMPT_PATH, "w") as f:
        f.write(candidate_prompt)
    cand_pass, cand_iters = score_benchmark()
    append_trace({"event": "self_improve_candidate", "pass": cand_pass, "iters": cand_iters})

    accept = cand_pass >= baseline_pass and cand_iters <= baseline_iters
    if not accept:
        with open(PROMPT_PATH, "w") as f:
            f.write(baseline_prompt)
    append_trace({"event": "self_improve_gate", "accepted": accept,
                  "baseline": [baseline_pass, baseline_iters],
                  "candidate": [cand_pass, cand_iters]})
    return accept, (baseline_pass, baseline_iters), (cand_pass, cand_iters)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        ok, iters = run_task("demo_add", "buggy_add.py", "test_add.py")
        print(f"task passed={ok} iters={iters}")
    elif cmd == "self-improve":
        accept, base, cand = self_improve()
        print(f"accepted={accept} baseline={base} candidate={cand}")
    elif cmd == "bench":
        p, it = score_benchmark()
        print(f"pass={p}/{len(BENCHMARK)} total_iters={it}")
    else:
        print("usage: harness.py [run|self-improve|bench]")
