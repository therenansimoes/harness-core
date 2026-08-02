# minimal self-improving harness

One file (`harness.py`), stdlib only, no dependencies to install.

## Run

```
python3 harness.py
```

Set `ANTHROPIC_API_KEY` to use a real model; without it, the agent uses a
deterministic offline fallback patch so the loop still runs end to end.

## What it does

1. **Agent turn**: writes a task test into `sandbox/test_task.py`, asks the
   model (or fallback) for a solution, writes it to `sandbox/solution.py`.
2. **Verifier**: runs `sandbox/test_task.py` as a plain Python script
   (asserts + exit code) — deterministic, no LLM judgment involved.
3. **Trace**: every turn appended to `trace.jsonl` (usage, latency, pass/fail).
4. **Self-improvement**: `self_improve()` reads the trace, proposes a change
   to `harness_config.json`, re-runs the verifier, and only writes the
   config if the oracle still passes. Reject path is exercised too.
5. **Safety**: `safe_write()` is the only way the harness writes files; it
   raises `PermissionError` for any path outside `sandbox/`. This is a
   Python path check, not a prompt instruction — verified in `NOTES.md`.

## Files

- `harness.py` — everything
- `sandbox/` — the only directory the agent may write into
- `trace.jsonl` — append-only turn log
- `harness_config.json` — self-improvement's only mutable target
