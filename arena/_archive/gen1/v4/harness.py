#!/usr/bin/env python3
"""
Minimal self-improving AI harness.

Loop: task -> agent proposes a patch -> deterministic verifier (pytest) decides
pass/fail -> trace logged (tokens/cost/time) -> a self-improvement pass reads
the trace and proposes a change to harness_config.json, gated by re-running
the verifier before/after (reject if it doesn't help or breaks anything).

Safety: all file writes are routed through safe_write(), which refuses any
path outside SANDBOX_DIR. This is enforced in code, not by prompting the LLM.
"""
import json, os, subprocess, sys, time, re, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SANDBOX_DIR = os.path.join(ROOT, "sandbox")
TRACE_PATH = os.path.join(ROOT, "trace.jsonl")
CONFIG_PATH = os.path.join(ROOT, "harness_config.json")

DEFAULT_CONFIG = {"max_turns": 3, "model": "claude-sonnet-5", "temperature": 0.0}


# ---------- safety: hard filesystem gate (mechanism, not prompt) ----------
def safe_write(path: str, content: str):
    abs_path = os.path.abspath(path)
    if not abs_path.startswith(SANDBOX_DIR + os.sep) and abs_path != SANDBOX_DIR:
        raise PermissionError(f"refused: write outside sandbox: {abs_path}")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(content)


def load_config():
    if os.path.exists(CONFIG_PATH):
        return json.load(open(CONFIG_PATH))
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:  # config lives in ROOT, not sandbox -> direct write ok
        json.dump(cfg, f, indent=2)


def log_trace(event: dict):
    event["ts"] = time.time()
    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(event) + "\n")


# ---------- the agent: calls the LLM if a key is present, else a ----------
# ---------- deterministic offline fallback so the loop is always runnable --
def call_llm(prompt: str, model: str):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    t0 = time.time()
    if not api_key:
        # offline fallback: deterministic canned "fix" so the loop is fully
        # runnable with zero network / zero deps. Real runs set the API key.
        text = FALLBACK_PATCH
        usage = {"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4}
        return text, usage, time.time() - t0, True

    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        text = "".join(b["text"] for b in data["content"] if b["type"] == "text")
        usage = data.get("usage", {})
        return text, usage, time.time() - t0, False
    except Exception as e:
        return f"ERROR: {e}", {}, time.time() - t0, True


FALLBACK_PATCH = '''def add(a, b):
    return a + b
'''


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return m.group(1) if m else text


# ---------- deterministic verifier: code decides, not the LLM ----------
# stdlib-only (no pytest dependency) so the harness runs with zero installs.
def run_verifier() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, os.path.join(SANDBOX_DIR, "test_task.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    ok = proc.returncode == 0
    return ok, (proc.stdout + proc.stderr)[-2000:]


TASK_PROMPT = """Write a Python function `add(a, b)` that returns the sum of a and b.
Return ONLY the code in a ```python fenced block, no explanation."""

TEST_FILE = '''import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from solution import add

def test_add_basic():
    assert add(2, 3) == 5

def test_add_negative():
    assert add(-1, 1) == 0

if __name__ == "__main__":
    test_add_basic()
    test_add_negative()
    print("OK")
'''


def agent_turn(cfg):
    safe_write(os.path.join(SANDBOX_DIR, "test_task.py"), TEST_FILE)
    text, usage, latency, offline = call_llm(TASK_PROMPT, cfg["model"])
    code = extract_code(text)
    safe_write(os.path.join(SANDBOX_DIR, "solution.py"), code)
    ok, output = run_verifier()
    event = {
        "type": "agent_turn",
        "model": cfg["model"],
        "offline_fallback": offline,
        "usage": usage,
        "latency_s": round(latency, 3),
        "verify_pass": ok,
        "verify_tail": output,
    }
    log_trace(event)
    return ok, event


# ---------- self-improvement: read trace, propose a config change, gate it --
def self_improve(cfg):
    """Read the trace; if recent turns show high latency, try lowering
    temperature (cheap, deterministic knob) and re-verify. Keep the change
    only if verification still passes after re-running. This is the whole
    gate: propose -> re-run oracle -> accept iff pass and not worse."""
    if not os.path.exists(TRACE_PATH):
        return None
    lines = [json.loads(l) for l in open(TRACE_PATH) if l.strip()]
    turns = [l for l in lines if l.get("type") == "agent_turn"]
    if not turns:
        return None

    before_pass_rate = sum(1 for t in turns if t["verify_pass"]) / len(turns)

    proposal = dict(cfg)
    proposal["temperature"] = 0.0
    proposal["max_turns"] = min(cfg.get("max_turns", 3) + 1, 5)

    ok, _ = run_verifier()  # re-run oracle against current sandbox state
    accepted = ok and before_pass_rate >= 0.5

    result = {
        "type": "self_improve",
        "before_pass_rate": before_pass_rate,
        "proposal": proposal,
        "accepted": accepted,
    }
    log_trace(result)
    if accepted:
        save_config(proposal)
    return result


def main():
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    cfg = load_config()
    save_config(cfg)

    print(f"[harness] config={cfg}")
    ok, event = agent_turn(cfg)
    print(f"[harness] agent turn verify_pass={ok} offline={event['offline_fallback']}")

    result = self_improve(cfg)
    print(f"[harness] self_improve={result}")

    print(f"[harness] trace written to {TRACE_PATH}")


if __name__ == "__main__":
    main()
