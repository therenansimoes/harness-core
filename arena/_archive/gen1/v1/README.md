# minimal self-improving agent harness

One command, no dependencies beyond `pytest` (already installed if you ran
the demo below).

## Run

```
python3 harness.py demo_task --max-turns 3
```

Runs the agent loop on `demo_task/` (a Python file with one bug and one
failing test). Loop: verify (pytest) -> if red, ask policy for a patch ->
apply patch inside a sandboxed path -> verify again -> stop on green or
`max-turns`. Trace of every turn is written to `demo_task/trace.jsonl`.

## Self-improvement

```
python3 evolve.py demo_task --rounds 3
```

Reads the trace/metric from a baseline run, proposes a change to
`config.json` (currently: `max_turns` budget), re-runs the task with the
candidate config in an isolated temp copy, and accepts the change **only**
if tests still pass and the metric (turns-to-green + failure penalty) does
not regress. Decisions are appended to `evolve_log.jsonl` — nothing here is
an LLM opinion, it's a numeric comparison in `evolve.py:metric_from_result`.

## Files

- `harness.py` — the agent loop.
- `policy.py` — proposes the next action. Uses the real Anthropic API if
  `ANTHROPIC_API_KEY` is set (`anthropic` package required); otherwise falls
  back to a deterministic offline stub (see NOTES.md).
- `verify.py` — deterministic verifier, runs `pytest`, exit code is the
  ground truth. No LLM in this path.
- `trace.py` — append-only JSONL trace per turn (tokens, cost, time, ok/fail).
- `safety.py` — invariants enforced in code: file writes cannot escape the
  task sandbox directory, shell commands are checked against a blocklist
  (network tools, destructive rm, git push, fork bombs) before execution.
- `evolve.py` — the self-improvement loop + gate.
- `demo_task/` — toy task used by both commands above.

## Extending to a real coding agent

Swap `policy._offline_action` calls for `policy._anthropic_action` by
setting `ANTHROPIC_API_KEY`; wire in the `anthropic` Python package
(`pip install anthropic`). The harness loop, verifier, trace, safety and
evolve gate do not change — only the policy backend does.
