# minimal-harness

Zero-dependency (stdlib-only, optional `anthropic`) demo of an autonomous
agent loop with deterministic verification, tracing, a gated self-improve
loop, and code-enforced safety invariants.

## Run

```
python3 harness.py                # runs the demo task, logs to trace.jsonl
python3 verify.py                 # just the deterministic verifier
python3 self_improve.py           # reads trace.jsonl, proposes+gates a change to harness.py
```

No install needed for the default (stub) mode. To use a real LLM instead of
the rule-based fallback:

```
export ANTHROPIC_API_KEY=sk-...
python3 harness.py
```

## Files

- `harness.py` — agent loop (LLM or stub) + trace logging
- `verify.py` — deterministic pass/fail (runs `workspace/test_task.py`, checks exit code)
- `safety.py` — path sandboxing + command allowlist, used by everything else
- `self_improve.py` — reads `trace.jsonl`, proposes a change to `harness.py`, gates it by re-running the demo task in a subprocess; rolls back on failure
- `workspace/` — the sandboxed task the agent works on (a deliberately buggy `add()`)
- `trace.jsonl`, `self_improve_log.jsonl` — append-only logs
