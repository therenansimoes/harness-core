# NOTES

## Approach
5-minute budget → single stdlib-only Python file, no framework, no install
step. Picked this over LangGraph/etc because setup/import overhead alone
would have eaten the clock; a from-scratch loop is small enough to be
fully real (not stubbed) in the time available.

## What works — verified by actually running it
- `python3 harness.py` runs end to end: agent turn → verifier → trace →
  self-improvement. Ran it twice, confirmed output both times.
- Deterministic verifier: `sandbox/test_task.py` runs as a plain script
  (assert-based, no pytest — pytest wasn't installed and I didn't want an
  install step burning time/risking network). Exit code decides pass/fail,
  not the LLM. Confirmed `verify_pass=True` in `trace.jsonl`.
- Trace: `trace.jsonl` has real per-turn records (usage tokens, latency_s,
  verify_pass, verify_tail). Confirmed by `tail`-ing the file.
- Self-improvement loop: `self_improve()` proposes a config change
  (max_turns +1), re-runs the verifier as the gate, and only writes
  `harness_config.json` if it passes. Confirmed the file was updated
  (`max_turns: 3 -> 4`) after acceptance.
- Safety gate: `safe_write()` is a hard code check, not a prompt. Tested
  directly: `safe_write('../evil.txt', 'x')` raises `PermissionError`,
  confirmed by running it and catching the exception.

## What's half-finished
- Only one toy task (`add(a,b)`) — no task variety, no multi-file tasks.
- No real LLM call path was exercised this run (no `ANTHROPIC_API_KEY` in
  this environment) — the offline fallback path is what's verified live;
  the `call_llm` HTTP path is written but untested against the real API.
- Self-improvement only tunes one numeric knob (`max_turns`) via one
  fixed heuristic — no search over multiple candidate changes, no
  rollback-if-regresses-later logic beyond the single gate check.
- No real gate rejection has been observed on a genuine regression (only
  tested the accept path and a synthetic reject condition).

## With 5 more minutes I would
- Add a second, harder task (e.g. one requiring a bugfix on existing code)
  so the verifier has to discriminate real quality, not just presence of
  a function.
- Make self-improvement propose an actual prompt/strategy change (not
  just a config number) and A/B it against the previous version over N
  tasks before accepting.
- Wire a real API call test (with a fake/mock key) to catch request-shape
  bugs in `call_llm` without spending real tokens.

## Biggest trade-off
Chose breadth-of-real-verification over breadth-of-features: every claim
in this file was actually executed and checked, but the surface area
(one task, one tunable knob, no live LLM call) is intentionally small so
that nothing here is fabricated.
