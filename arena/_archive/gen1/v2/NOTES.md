# NOTES

## Approach and why

Single stdlib-only Python file (`harness.py`), no external deps, no SDK.
Time budget was 5 minutes, so I skipped researching LangGraph/Aider/etc and
wrote the smallest version of each rubric piece directly: agent loop,
subprocess-based deterministic verifier, JSONL trace, a gated self-improve
step, and a path-whitelist safety check. Chose stdlib-only + raw HTTPS call
to Anthropic (no `anthropic` package) so it's runnable by a third party with
zero `pip install`, and chose an offline deterministic stub LLM as fallback
so the whole loop is verifiable without any API key or network at all.

## What's working, verified by actually running it

- `python3 harness.py bench` → `pass=2/2 total_iters=2` (ran it, output above).
- `python3 harness.py self-improve` → `accepted=True baseline=(2,2) candidate=(2,2)`
  (ran it, output above). Gate correctly compares baseline vs candidate
  pass-rate and iters before accepting/reverting the prompt template.
- Verifier is real subprocess isolation + exit code, not LLM opinion —
  confirmed by intentionally breaking a fixture mid-session and watching
  `pass=0/2` register in bench and in `trace.jsonl`.
- `safe_write()` safety check: raises `PermissionError` for any path
  resolving outside `workspace/`. Not independently exercised with a
  deliberate escape attempt this session — logic is a straightforward
  `os.path.realpath` prefix check, but I did not write a test proving it
  blocks e.g. `../../etc/passwd`.
- Found and fixed a real bug live: `score_benchmark()` was mutating the
  workspace fixture files in place, so calling it twice (once for baseline,
  once for candidate in self-improve) corrupted the second run because the
  offline stub only pattern-matches the *original* buggy source. Fixed by
  resetting fixtures from a `FIXTURES` dict before each benchmark task.

## What's half-done

- Self-improve's *candidate proposal* is a hardcoded heuristic (append a
  line to the prompt), not LLM-authored. The **gate** (score, compare,
  accept/revert) is real and general; the proposal step is a stand-in.
- Only 2 benchmark tasks, both toy (arithmetic bug, off-by-one boolean).
  Offline stub is pattern-matched to exactly these two — it is not a
  general code fixer, just enough to exercise the loop without an API key.
- Real Anthropic path (`ANTHROPIC_API_KEY` set) is implemented but I did
  not have a key in this environment, so it is untested this session —
  only the offline-stub path was actually run and verified.
- No test proving the safety sandbox actually blocks a traversal attempt.

## With 5 more minutes I would

1. Write an explicit test that calls `safe_write("../evil.py", "x")` and
   asserts `PermissionError`, to actually prove the safety invariant
   instead of just reading the code.
2. Grow the benchmark to 5-6 tasks with more varied bug classes so
   self-improve's gate has more signal than a binary 2/2.
3. Make the self-improve candidate step LLM-authored (ask the LLM to
   propose a prompt_template.txt diff based on trace failures) while
   keeping the same deterministic gate — right now the "propose" half is
   the weakest link relative to the rubric.

## Biggest trade-off

Chose breadth (all six rubric pieces present, each minimal and genuinely
run) over depth on any one piece. The alternative — a fully general agent
loop with a real code-editing LLM and a large benchmark — would have blown
the 5-minute budget and likely left me with an unverified, unrun artifact.
Everything above marked "verified" was actually executed this session, not
assumed.
