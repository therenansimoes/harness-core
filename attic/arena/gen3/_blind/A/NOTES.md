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

## Fase 2 — what I added, verified by executing `./run.sh` after each change
- **`--repo <dir> --test-cmd "<cmd>"`** (harness.py): the harness now accepts
  a third-party project and uses THAT project's own test command as the
  oracle, instead of assuming pytest + its own fixture. `verify.run_tests`
  takes an optional `test_cmd` (a real argv list, still routed through
  `safe_run` — no `shell=True`, allowlist still applies to someone else's
  command). run.sh step "4b" proves this end-to-end against a project this
  harness never saw before: a `src/` package layout (not flat, unlike the
  fixture) tested with `python3 -m unittest -v` (not pytest). Fixed a
  different seeded bug (`w*h+1`) in 1 turn — `"success": true` in the
  captured JSON, not asserted.
- `policy._candidate_files` now walks recursively (`rglob`, skipping
  `.git`/`node_modules`/venvs) instead of a flat `glob("*.py")` — the flat
  version silently found zero candidates for the `src/`-nested third-party
  repo and always reported `success: false`. This was caught by actually
  running the new run.sh step, not by inspection.
- `verify.hash_tests`'s tamper gate widened from pytest's `test_*.py` only
  to also recognize `*_test.py`, `*.test.js`, `*.spec.js`, `*.test.ts`,
  `*.spec.ts`, `*_test.go` — a third-party repo won't necessarily use
  pytest's naming convention, and an un-hashed test file is a silent hole
  in the tamper invariant for anyone pointing this at their own code.
- **Closed the known safety hole** from this file's own "Biggest trade-off"
  section: `safety.guard_command` used to allow-list by binary name only,
  so `python3 -c "os.system(...)"` sailed through as a "python3" call.
  Now `-c` is rejected outright for python/python3, and `-m` is restricted
  to `pytest`/`unittest` — closes the exact escape called out below, kept
  the allowlist model instead of switching to a heavier sandbox (still no
  kernel-level isolation, see remaining gap below). Verified by re-running
  run.sh step 7 (still passes) plus a manual `safe_run(["python3","-c",
  "import os; os.system('id')"], ...)` call that now raises
  `SafetyViolation` — confirmed by executing it, not asserted from reading
  the diff.

## Half-finished / cut for time
- Trace cost tracking is real but only exercised for the `anthropic` backend
  path (untested here — no API key in this environment, by design per the
  brief). Offline runs correctly report `cost_usd: 0.0`, not a fake number,
  but I did not get to add a second live smoke test against a real API key.
- Mutation catalog in `policy.py` is 12 hand-picked operator swaps. It will
  not find multi-line or multi-symbol bugs — it's breadth over a narrow bug
  class (single operator / off-by-one), not a general program repair engine.
- No sandbox process isolation (container/seccomp) — safe_run relies on the
  argv allowlist + realpath guard + no shell=True + (now) a `-c`/`-m` filter
  on python invocations, not a hard kernel boundary. `python3 -m unittest`
  running arbitrary third-party `setUp()`/module-level code is still trusted;
  only the *inline* `-c` escape and arbitrary `-m <module>` are closed. A
  determined third-party repo could still run malicious code from inside its
  own test suite — that's inherent to "run someone else's test command" and
  would need a real container/seccomp boundary to fully close.
- `--repo`/`--test-cmd` baseline is NOT git-stash-isolated: if the agent's
  edit makes things worse there's no automatic revert of the third-party
  repo's working tree (the fixture path uses disposable tmp copies instead,
  which sidesteps this). For a real repo you'd want `git stash` before/after
  each turn — noted, not implemented, the demo instead uses a throwaway
  tempdir so nothing of value is ever at risk.
- No `--task-dir` docs/README beyond this file; CLI args are minimal.

## Biggest trade-off
Chose a mutation-search offline policy over reading fixture hints (gen1's
universal failure) — costs more code and only handles one bug class, but it
is the difference between "verification measures a lookup table" and
"verification measures a search that could fail." Proven it can fail: on
reversed order the candidate needed 5 turns instead of 1, which is exactly
the signal evolve.py's gate uses to reject.

## With 5 more minutes
1. ~~Close the `python3 -c "os.system(...)"` escape~~ — done in Fase 2
   (`safety.guard_command` now rejects `-c` and non-pytest/unittest `-m`).
2. ~~Add a `--test-cmd` override~~ — done (`harness.py --repo --test-cmd`,
   demonstrated end-to-end against an unseen `src/`-layout unittest project).
3. Wire `git stash` (or a full disposable clone) as the baseline/rollback
   mechanism for `--repo` against a REAL git checkout, so a bad agent turn
   on someone's actual repo reverts automatically instead of leaving edits
   in the working tree.
4. Extend the mutation catalog to multi-token patches (swap two args, off-by
   constants beyond 1) and add a second, harder fixture bug to prove the
   search generalizes further than one bug class.
5. Non-Python `--test-cmd` support (e.g. `npm test`) needs the allowlist
   extended (`node`, `npm`, `go`) with the same "no arbitrary inline eval"
   discipline just added for python — currently only python-family repos
   can use `--repo`.
