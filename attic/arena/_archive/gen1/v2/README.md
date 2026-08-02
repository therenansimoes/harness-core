# Minimal self-improving agent harness

Stdlib-only Python. One file: `harness.py`.

## Run

```
python3 harness.py run           # single demo task: fix workspace/buggy_add.py
python3 harness.py bench         # run the fixed 2-task benchmark, report pass rate
python3 harness.py self-improve  # propose+gate a change to prompt_template.txt
```

No API key needed to run end-to-end: without `ANTHROPIC_API_KEY` set, an
offline deterministic stub LLM is used (it fixes the two known benchmark
bugs by pattern match) so the loop is fully verifiable without network.
With `ANTHROPIC_API_KEY` set, real Claude calls are made via the raw HTTPS
API (no SDK dependency).

## Pieces (mapped to the harness rubric)

- **Agent loop**: `run_task()` — reads a buggy file, calls the LLM, writes
  the fix, verifies, retries up to `max_iters`.
- **Deterministic verification**: `verify()` — spawns a subprocess that
  imports the module and calls every `test_*` function; pass/fail comes
  from the process exit code, not from LLM judgment.
- **Trace + measurement**: `append_trace()` — every LLM call and gate
  decision is appended to `trace.jsonl` with tokens, backend, wall-clock
  time, and a hash of verifier output.
- **Self-improvement loop + gate**: `self_improve()` — scores the current
  prompt template against the fixed `BENCHMARK`, proposes a candidate
  change, scores the candidate, and only accepts it (overwrites
  `prompt_template.txt`) if pass-rate doesn't regress and iters-to-pass
  doesn't increase. Rejected candidates are reverted.
- **Safety invariant with mechanism**: `safe_write()` — every file write
  resolves the target path and refuses anything outside `workspace/`,
  enforced in code (`PermissionError`), not by prompt instruction.

## Known limitations (see NOTES.md)

- Only two benchmark tasks; offline stub only knows how to fix those two
  specific bugs (it's a stand-in for a real LLM, not a general fixer).
- Self-improve's candidate-generation step is a hardcoded heuristic, not
  an LLM-authored diff — the *gating* mechanism is real, the *proposal*
  step is a placeholder for one.
