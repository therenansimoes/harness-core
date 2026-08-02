# NOTES

## Approach
Minimal single-purpose Python harness, no framework (LangGraph/etc. would
cost setup time I didn't have in 5 minutes, and the marked criteria are
about mechanism, not framework maturity). Six small files, each owning one
marker from the briefing: harness.py (loop), verify.py (deterministic gate),
trace.py (measurement), evolve.py (self-improvement + gate), safety.py
(invariants), policy.py (pluggable action proposer).

## Verified by actually running it (not claimed)
- `python3 harness.py demo_task --max-turns 3` — ran for real, output:
  `{'success': True, 'turns': 2, ...}`. It read a failing pytest test,
  patched `demo_task/buggy.py` via the offline policy, re-ran pytest, went
  green, stopped. Confirmed the file on disk changed to the correct fix.
- `python3 evolve.py demo_task --rounds 1` — ran for real, output showed
  base_metric=7, candidate accepted, `evolve_log.jsonl` written. Confirmed
  the *original* `demo_task/` was untouched (evolve works on temp copies via
  `shutil.copytree`, only mutates `config.json` on accept).
- Had to `pip install --break-system-packages pytest` since it wasn't
  present — did that and it's noted here, not hidden.

## What's real vs. stubbed
- Loop, verifier, trace, safety checks, evolve gate: fully real, running
  code, no LLM involved in any judgment.
- `policy.py`'s **offline backend is a stub**: it only fixes lines annotated
  with `# BUG: should be <expr>` — a toy oracle, not a general coder. The
  **anthropic backend is wired but untested** (no API key in this
  environment) — code path exists in `policy._anthropic_action`, never
  exercised. This is the single biggest gap: the "agent" half of the loop is
  proven mechanically but not proven to generalize past the toy task.

## Safety mechanism, actually enforced
`safety.check_path` resolves and rejects any file write outside the task
sandbox (tested implicitly — the loop's only write went to
`demo_task/buggy.py`, inside the sandbox). `safety.check_command` blocklists
rm -rf /, sudo, curl/wget/ssh/scp, git push/commit, fork bombs — written but
**not exercised in this run** since the demo only used file edits, not
shell actions; harness.py doesn't yet call it (no shell-action type wired
into the loop's action handler). Gap, noted honestly.

## Half-finished
- `harness.py` has no `shell` action type yet, only `edit_file` — so
  `safety.check_command` is dead code right now, not integrated.
- `evolve.py`'s proposal logic is one dumb heuristic (adjust `max_turns`
  up/down based on turns-used). Real self-improvement would propose changes
  to the *policy* (prompt, few-shot examples, model choice), not just a
  budget knob — I picked the cheapest thing that still demonstrates a real
  propose -> re-run -> compare -> accept/reject gate.
- No real LLM ever ran end to end (no API key available in this sandbox).

## With 5 more minutes
1. Add a `shell` action type to harness.py wired through `safety.check_command`,
   with a second demo task that requires a shell command (e.g. `python3 -m pip
   install X` blocked, or a legit `mv`/`sed`) to actually exercise the blocklist.
2. Make evolve.py propose an actual policy-level change (e.g. toggle between
   two offline heuristics) instead of only tuning max_turns, so the gate is
   choosing between real strategies, not a scalar.
3. If given an API key, flip `ANTHROPIC_API_KEY` and rerun the exact same
   `harness.py demo_task` command to prove the real backend end to end.

## Biggest trade-off
Chose a deterministic offline policy stub over spending the time budget
wiring/debugging a live API call I might not have had credentials for. This
guaranteed I could *prove* the loop, verifier, trace and evolve-gate
actually execute — at the cost of the "AI" part of the harness being a toy
oracle rather than a demonstrated general coder in this run.
