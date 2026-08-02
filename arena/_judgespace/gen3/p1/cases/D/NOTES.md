Crossover of gen1 v3 (mutates own code, real rollback) x gen1 v1 (hermetic
measurement, plugin policy). Verified by running `./run.sh` twice in a row
(idempotent, both exit 0) with under 90s left on the clock.

## What works, verified by executing it
- `harness.py <task_dir>`: agent loop, offline mutation-search policy
  (`policy.py`) that does NOT read any `# BUG:` hint and has no foreknowledge
  of the seeded bug — it tries a catalog of generic operator/off-by-one
  mutations against a scratch copy and keeps the first one whose hermetic
  test run goes green. Fixes `fixture/task` (off-by-one in `mean()`) in 1
  turn, AND fixes a different bug (`w*h+1` instead of `w*h`) in a throwaway
  third-party-style directory built fresh in `run.sh` step 4 — same code,
  never seen that bug before, so the search is real, not calibrated.
- `verify.py`: deterministic (no LLM). SHA-256 of every `test_*.py` before
  and after each turn — step 8 in run.sh proves an agent that edits the test
  file to cheat is a hard FAIL even though pytest itself would report green.
- `evolve.py`: self-improve loop. Proposes reversing the mutation search
  order, measures baseline vs candidate in two independent `tempfile.mkdtemp`
  copies (never the live fixture), accepts only on STRICT improvement
  (`<`, not `<=`). run.sh proves BOTH directions: round A starts from a
  deliberately bad order and the reversal is genuinely better -> accepted;
  round B proposes reversing back to the bad order -> genuinely worse ->
  rejected, config rolled back (never written). Both are in `evolve_log.jsonl`
  after running — not asserted, produced.
- `safety.py`: `guard_path` uses `realpath` (resolves symlinks — fixes v3's
  actual bug, verified in run.sh step 7 with a live `/etc` symlink escape
  attempt that abspath would have missed). `safe_run` uses an ALLOWLIST of
  argv[0] binaries (`python3(.\d+)?`, `pytest`, `sh`) instead of the substring
  denylist all 5 gen1 candidates shared — a `curl` call is rejected in step 7.

## Half-finished / cut for time
- Trace cost tracking is real but only exercised for the `anthropic` backend
  path (untested here — no API key in this environment, by design per the
  brief). Offline runs correctly report `cost_usd: 0.0`, not a fake number,
  but I did not get to add a second live smoke test against a real API key.
- Mutation catalog in `policy.py` is 12 hand-picked operator swaps. It will
  not find multi-line or multi-symbol bugs — it's breadth over a narrow bug
  class (single operator / off-by-one), not a general program repair engine.
- No sandbox process isolation (container/seccomp) — safe_run relies on the
  argv allowlist + realpath guard + no shell=True, not a hard kernel boundary.
  **CLOSED in this session**: `guard_command` in `safety.py` now pins the
  *argument shape*, not just the binary name — any `python*` invocation must
  be exactly `-m pytest ...` (the only legitimate use in this harness), and
  bare `sh` is rejected outright. `python3 -c "import os; os.system(...)"`,
  `python3 arbitrary_script.py`, and bare `sh` are all now `SafetyViolation`s,
  proven blocked in `run.sh` step 7b (executed, not asserted — see the
  `OK ... blocked` lines in the run.sh output). Still not a kernel boundary
  (no container/seccomp), but the specific escape named in the brief is shut.
- No `--task-dir` docs/README beyond this file; CLI args are minimal.

## Biggest trade-off
Chose a mutation-search offline policy over reading fixture hints (gen1's
universal failure) — costs more code and only handles one bug class, but it
is the difference between "verification measures a lookup table" and
"verification measures a search that could fail." Proven it can fail: on
reversed order the candidate needed 5 turns instead of 1, which is exactly
the signal evolve.py's gate uses to reject.

## With 5 more minutes (remaining, after closing the security hole above)
1. Extend the mutation catalog to multi-token patches (swap two args, off-by
   constants beyond 1) and add a second, harder fixture bug to prove the
   search generalizes further than one bug class.
2. Add a tiny `--test-cmd` override so the harness can point at a real
   third-party repo's actual test runner (e.g. `npm test`) instead of
   assuming pytest.
3. Wire real token/cost tracking for the `anthropic` backend path (currently
   only the offline path is exercised, correctly reporting 0.0 not a fake
   number, but never tested against a live API key in this environment).
