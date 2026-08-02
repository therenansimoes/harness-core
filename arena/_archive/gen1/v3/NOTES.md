# NOTES

## Approach

Single-file-per-concern Python, stdlib only, no framework. With 5 minutes,
reusing LangGraph/etc. would have cost more time reading docs than it saved
writing code — the whole harness is ~250 lines. Went for a small real
end-to-end loop over a big scaffolded-but-untested one.

## What actually works (executed, not assumed)

- `python3 harness.py` — real loop: reads `workspace/task.py` (buggy `add`),
  proposes a fix, applies it, runs `verify.py` (subprocess, exit-code based,
  zero LLM involvement), logs a trace record. Ran it, watched it fix the bug
  and print `TASK SOLVED`, confirmed `workspace/task.py` was rewritten.
- `verify.py` — deterministic. Ran it standalone against both broken and
  fixed code, got `FAIL`/`PASS` correctly both times.
- Trace — `trace.jsonl` has real records with step, model id, token usage,
  latency, patch_applied, verify_passed. Inspected the file after each run.
- `self_improve.py` — ran it twice, both genuine:
  1. Against an *unfixable* bug (stub agent can't solve it): proposed
     `increase_max_steps`, applied it, gate re-ran `harness.py` fresh in a
     subprocess, gate failed (task still unsolved), harness.py was rolled
     back via the backup file. Confirmed `harness.py` was unchanged after.
  2. Against an already-solved-in-1-step trace: correctly proposed nothing
     (`no change proposed`) rather than mutating for the sake of it.
- `safety.py` — `guard_path` rejects anything resolving outside this dir;
  `safe_run` takes argv lists only (no `shell=True`, no injection surface)
  and blocks a substring denylist (`rm -rf`, `git push`, `sudo`, network
  tools, fork bombs, etc.). Not fuzzed, but the path-escape and shell=True
  classes of bypass are structurally closed off.
- No network calls were made — no `ANTHROPIC_API_KEY` was set in this
  environment, so every run above used the stub heuristic patcher, clearly
  labeled `model: "stub:heuristic-patcher"` in every trace record. The real
  Anthropic call path (`call_llm_real`) exists and is wired in but is
  **untested** — I did not fabricate a run of it.

## What's half-built

- The self-improvement "proposal" is a fixed, narrow policy (bump
  `max_steps`), not free-form LLM-authored patches to the harness itself.
  I judged unattended free-form self-rewrite too risky to ship untested in
  the time available — the gate mechanism (backup → apply → re-run →
  keep-or-rollback) is real and generalizes, but the proposal generator
  is a stub, same honesty caveat as the agent's LLM path.
- Only one demo task (fix `add`). No task-generality testing.
- No cost tracking beyond raw token counts (no $ conversion table).
- `safety.py` denylist is substring-based, not a real sandboxing primitive
  (no seccomp/container). Good enough to stop the obvious footguns, not a
  security boundary against an adversarial model.

## With 5 more minutes

1. Wire and actually test `call_llm_real` with a live key so trace records
   an authentic model-based fix, not just the stub.
2. Make `propose_change` read trace stats more richly (e.g., propose
   swapping stub heuristics based on repeated `patch_applied=False` on the
   same bug signature) instead of one hardcoded mutation.
3. Add a second, harder demo task so "verification" isn't trivially gameable
   by a heuristic tuned to the one bug shown.

## Biggest trade-off

Chose a fully-real, narrow loop over a broad, partially-faked one. The
self-improve step in particular could have "looked" more impressive with an
LLM writing arbitrary diffs to harness.py, but I couldn't verify that path
would behave safely in the time left, so I kept it to a deterministic,
gated, rollback-safe mutation instead — smaller claim, actually checked.
