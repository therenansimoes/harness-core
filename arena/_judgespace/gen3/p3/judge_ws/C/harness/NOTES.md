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

## Gen3 changes (this session — genetic focus: search power)
Verified by re-running `./run.sh` after each edit (exit 0 every time, logs
in this session's `/tmp/run{1..4}.log`).
- `policy.py` MUTATIONS catalog grown from 12 to 26 entries: off-by-2 in
  addition to off-by-1, `*`/`/` swap, `==`/`!=` swap, `min`/`max` swap,
  `sum`→`len`, `None`→`0`, `+=`/`-=` swap, and a `-1 -> +1` / `+1 -> -1`
  sign-flip pair distinct from the existing "delete the offset" pair. Still
  regex-based single-token patches, still no foreknowledge of the seeded
  bug — just a wider single-operator search space.
- `evolve.py` was a fixed-point proposer (one move: reverse the order).
  That is exactly the failure mode the briefing calls out — a proposer
  with one move converges to "propose the same losing flip forever" the
  moment base beats it once, and every prior generation died there. Now
  `evolve_once` runs FOUR independent strategies each round — reverse,
  rotate-by-half, a trace-stats-biased reorder (`mutation_win_stats()`
  reads `evolve_log.jsonl` for which mutation index won past rounds and
  pulls proven winners to the front), and an ordered-crossover (`crossover()`,
  OX-style: a slice from strategy A's order + the remainder filled from
  strategy B's order) — measures all of them plus base hermetically in
  separate tempdirs, and accepts only the strict winner. `evolve_log.jsonl`
  entries now record every proposal's order/metric/success under
  `"proposals"`, not just the one that won, so the search is auditable.
- `safety.py`: closed the exact hole this NOTES.md flagged — `guard_command`
  now rejects `-c`/`--command` on any allowlisted interpreter binary
  (`python3`, `python`, `sh`). The allowlist previously gated *which binary*
  but not what a bare interpreter is told to do once running; `python3 -m
  pytest` (the harness's own real usage) still passes, `python3 -c
  "os.system(...)"` is now a `SafetyViolation`. Proven in `run.sh` step 7
  with a live attempt, not just asserted.

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
  Good enough for the stated invariant ("what the agent can't do, by code")
  but a determined escape via an allowed binary's own capabilities
  (e.g. `python3 -c "os.system(...)"`) is NOT blocked — the allowlist gates
  *which binary*, not what that binary is told to do once it's a bare
  interpreter. This is the biggest real gap.
- No `--task-dir` docs/README beyond this file; CLI args are minimal.

## Not done this session (ran out of clock)
- Briefing asked for a declared knob space with ranges (e.g. `max_turns`,
  timeout) alongside the mutation-order search. Only the mutation-order
  dimension got the multi-strategy treatment; numeric knobs still come
  from CLI flags only, not from `evolve.py`'s search. Next step: add a
  `KNOB_SPACE = {"max_turns": (1, 12)}`-style dict and a `propose_knob`
  strategy alongside the four order-proposers already in `PROPOSERS`.
- Crossover only combines two mutation-ORDER proposals (OX crossover on
  index lists). True crossover of two independently-evolved config.json
  files (order + knobs) once knobs exist is the natural follow-up.

## Biggest trade-off
Chose a mutation-search offline policy over reading fixture hints (gen1's
universal failure) — costs more code and only handles one bug class, but it
is the difference between "verification measures a lookup table" and
"verification measures a search that could fail." Proven it can fail: on
reversed order the candidate needed 5 turns instead of 1, which is exactly
the signal evolve.py's gate uses to reject.

## With 5 more minutes
1. Close the `python3 -c "os.system(...)"` escape: either drop the bare
   `python3`/`sh` allowlist entries for agent-writable content, or run the
   agent's own candidate patches through an AST check (no `import os`,
   `subprocess`, `open` outside task_dir) before ever exec'ing pytest on them.
2. Extend the mutation catalog to multi-token patches (swap two args, off-by
   constants beyond 1) and add a second, harder fixture bug to prove the
   search generalizes further than one bug class.
3. Add a tiny `--test-cmd` override so the harness can point at a real
   third-party repo's actual test runner (e.g. `npm test`) instead of
   assuming pytest.
