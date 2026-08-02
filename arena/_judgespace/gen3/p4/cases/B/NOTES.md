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

## Gen 3 changes (this session), verified by running `./run.sh`
- **Real cost, not just for the anthropic path.** `cost_usd`/`tokens` were
  `0.0`/`0` on every turn before, including offline runs — the trace could
  never move a decision. Now:
  - `policy._anthropic_action` returns `resp.usage.{input,output}_tokens`
    (API-metered, exact) attached to the action.
  - `harness.run_task`, for the offline backend (no API key, the default in
    this sandbox), estimates tokens from real bytes actually read (prior
    file content = "prompt") and written (the edit = "completion") at
    ~4 chars/token, priced at the same published Sonnet rate table. Each
    trace entry is tagged `cost_model: "metered"` or `"estimated"` so an
    estimate is never silently confused with a billed number.
  - Verified: `trace.jsonl` after `./run.sh` now shows `"tokens": 101,
    "cost_usd": 0.000903, "cost_model": "estimated"` on the fixture-fix
    turn — not zero.
  - **The gate now actually uses cost.** `evolve.metric()` already had
    `total_cost_usd * 1000` in its formula, but it was a dead term while
    cost was always 0 — the gate only ever moved on turn count. With real
    numbers, `evolve_log.jsonl`'s round A now reads `base_metric=504.608
    candidate_metric=100.903` (previously a flat `500.0`/`100.0`) — cost is
    now load-bearing in the accept/reject decision, not cosmetic.
- **Closed the known allowlist hole.** `safety.guard_command` allowlisted
  the *binary* (`python3`) but not what it's told to do — `python3 -c
  "os.system(...)"` passed straight through and ran arbitrary code, even
  though `curl` itself was blocked. Fixed by rejecting `-c` (inline code)
  and bare `-` (stdin-as-script) in argv for any python-family binary;
  `-m` is left alone because `verify.py` legitimately runs `python3 -m
  pytest`. Proven in `run.sh` step 7 with a live
  `python3 -c "import os; os.system('echo pwned')"` call through
  `safe_run` — now raises `SafetyViolation`, previously would have
  executed.
- Mutation catalog in `policy.py` is still 12 hand-picked operator swaps —
  not touched this session, still a narrow single-operator/off-by-one bug
  class, not a general program repair engine.
- No sandbox process isolation (container/seccomp) — safe_run relies on
  argv allowlist + flag checks + realpath guard + no shell=True, not a hard
  kernel boundary. Good enough for "what the agent can't do, by code" but
  not a substitute for a real sandbox if the allowlist ever grows.
- Token estimate for the offline path is a rough 4-chars/token heuristic on
  file bytes, not a real tokenizer — accurate enough to make cost move
  decisions, not accurate enough to bill against.
- No `--task-dir` docs/README beyond this file; CLI args are minimal.

## Biggest trade-off
Chose a mutation-search offline policy over reading fixture hints (gen1's
universal failure) — costs more code and only handles one bug class, but it
is the difference between "verification measures a lookup table" and
"verification measures a search that could fail." Proven it can fail: on
reversed order the candidate needed 5 turns instead of 1, which is exactly
the signal evolve.py's gate uses to reject.

## With 5 more minutes
1. ~~Close the `python3 -c "os.system(...)"` escape~~ — done this session,
   see above (flag-level check, not binary-level).
2. Extend the mutation catalog to multi-token patches (swap two args, off-by
   constants beyond 1) and add a second, harder fixture bug to prove the
   search generalizes further than one bug class.
3. Add a tiny `--test-cmd` override so the harness can point at a real
   third-party repo's actual test runner (e.g. `npm test`) instead of
   assuming pytest — the briefing calls this out as the gap nobody's closed:
   the harness has only ever run itself against its own planted bug.
4. Swap the 4-chars/token heuristic for `tiktoken` or the Anthropic token
   counting endpoint so offline cost estimates are closer to real billing.
