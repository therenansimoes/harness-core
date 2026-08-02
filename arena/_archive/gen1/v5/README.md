# minimal self-improving agent harness

Stdlib-only Python (no deps, no network, no API key required).

## Run

```
python3 harness.py        # agent loop: tries strategies against tasks/*, verifies, traces
python3 self_improve.py   # reads trace.jsonl, proposes a strategy reorder, gates on measured turns
```

## What it does

- `harness.py`: for each task in `tasks/`, tries fix strategies (from `strategies.json`)
  in order until `test_solution.py` exits 0. Every turn appended to `trace.jsonl`
  (task, strategy, pass/fail, duration, wall time). Failed patches are reverted;
  passing patches are kept (the task is actually fixed, not just scored).
- `verify()`: deterministic — runs `test_solution.py` as a subprocess, exit code
  decides pass/fail. No LLM ever judges its own work.
- `self_improve.py`: ranks strategies by historical win count in `trace.jsonl`,
  proposes a new order, then actually re-runs the full suite with the proposed
  order and only keeps it if total turns-to-solve-all strictly improves and
  nothing regresses to unsolved. Otherwise it reverts `strategies.json`.
- `_safe_path()` in harness.py refuses any write outside this directory
  (the sandbox invariant — enforced by code, not by prompt wording).

## Caveat (see NOTES.md)

Passing patches are kept, so re-running `harness.py` against already-fixed
tasks trivially solves in 1 turn each. To see the gate do real work again,
restore `tasks/*/solution.py` to a buggy state first.
