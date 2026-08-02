Crossover of gen1 v3 (mutates own code, real rollback) x gen1 v1 (hermetic
measurement, plugin policy). Verified by running `./run.sh` twice in a row
(idempotent, both exit 0) with under 90s left on the clock.

## Fase 2 — changes made, each verified with a fresh `./run.sh` (exit 0 every time)
- `policy.py`: extended `MUTATIONS` from 12 to 18 (added `*`/`/` swap,
  `None`->`0`, `min`/`max` swap, `.append`->`.remove`). Same offline
  mutation-search mechanism, wider bug-class coverage.
- `safety.py`: closed the exact hole this NOTES.md previously flagged as
  "biggest real gap" — `python3 -c "os.system(...)"` and `python3 -m
  <arbitrary>` passed the argv[0] allowlist because it only checked which
  *binary* ran, not what a bare interpreter was told to do. `guard_command`
  now rejects `-c` outright and restricts `-m` to an explicit
  `ALLOWED_MODULE_INVOCATIONS` set (`{"pytest"}`) — matches the only real
  call shape in this codebase (`verify.py`'s `python3 -m pytest`).
  Proved live in `run.sh` step 7, not just asserted: the `-c` escape is
  blocked, the `-m http.server` escape is blocked, AND the legitimate
  `python3 -m pytest` call is confirmed still allowed (regression check —
  a fix that also broke verify.py would have failed run.sh immediately).

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
  argv allowlist + realpath guard + `-c`/`-m` flag check + no shell=True, not
  a hard kernel boundary. The `-c`/bare-`-m` escape is now closed (see Fase 2
  above), but this is still not a kernel-level sandbox: an allowlisted
  `pytest` run of attacker-controlled test code can still do anything a
  normal Python process can (no seccomp/container/rlimit). Good enough for
  the stated invariant ("what the agent can't do, by code") for this
  harness's own gate, not a hard boundary against a hostile third-party repo.
- No `--task-dir` docs/README beyond this file; CLI args are minimal.
- Trace/measurement (weight 20 per briefing) is still the weakest marco:
  `tokens: 0`, `cost_usd: 0.0` on every offline turn. Did not get to this in
  Fase 2 — ran out of time after the safety fix and catalog extension. This
  is the single highest-leverage next step.

## Biggest trade-off
Chose a mutation-search offline policy over reading fixture hints (gen1's
universal failure) — costs more code and only handles one bug class, but it
is the difference between "verification measures a lookup table" and
"verification measures a search that could fail." Proven it can fail: on
reversed order the candidate needed 5 turns instead of 1, which is exactly
the signal evolve.py's gate uses to reject.

## With 5 more minutes
1. Wire real token/cost measurement into `trace.jsonl` even for the offline
   backend (count characters/lines touched as a proxy metric, or at minimum
   make the `anthropic` backend path populate `usage.input_tokens` /
   `usage.output_tokens` from the real API response — right now those fields
   are just never set on that path either, so the number would be fake if
   asserted without running it against a real key).
2. Extend the mutation catalog further (multi-token patches, swap two args)
   and add a second, harder fixture bug to prove the search generalizes
   further than one bug class.
3. Add a tiny `--test-cmd` override so the harness can point at a real
   third-party repo's actual test runner (e.g. `npm test`) instead of
   assuming pytest.
4. Kernel-level isolation (container/seccomp/rlimit) for the actual pytest
   subprocess, not just the argv allowlist — the `-c`/`-m` fix in Fase 2
   closes the interpreter-flag bypass but does not sandbox what an
   allowlisted `pytest` run of untrusted test code can do once it's running.
