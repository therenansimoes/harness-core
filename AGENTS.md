# AGENTS.md — constitution for any AI agent working in this repo

Applies to Claude Code, Cursor, the harness's own autopilot, and humans in a
hurry. Source of truth for state: `STATUS.md`. Source of truth for the genome:
`config/genome.toml`.

## What the repo is

A self-evolving agent harness on LangGraph. The `harness/` package
(vendor-agnostic core; default executor deepagents, optional `claude_code`
backend) runs units of work through a graph (`plan → route → provision → execute
→ verify → measure → gate → record`), measures KPIs against a frozen baseline,
and evolves itself through 9 registered actions (`research`, `codegen`,
`synthesize`, `redteam`, `topology`, `workflow`, `evolve`, `skill_prune`,
`prompt`) — always inside the genome's mutable zones, judged by a ruler it cannot
touch. Ledger in `data/runs.sqlite`; `legacy/` is frozen read-only reference,
outside pytest and outside the genome.

## Genome zones — never edit without a human

Mirror of `config/genome.toml` (which is also enforced fail-closed at runtime):

- `harness/ruler/**` — whoever measures and decides does not get to change
  itself: a mutation that rewrites the ruler approves itself.
- `harness/genome/**` — whoever defines what may change does not change either.
- `harness/routing/**` — whoever picks the model does not change itself,
  otherwise a proposal hands itself the expensive tier and fakes the A/B.
- `harness/graph/**` — the topology is the process; the loop calibrates the TOML,
  not the nodes.
- `uv.lock` — pinned deps: swapping a version underneath invalidates every
  comparison.
- `benchmarks/sealed/**` — the sealed exam: if the loop rewrites the test, the
  grade is worth nothing.

Mutable (where an agent may operate): `config/*.toml`, `config/workflows/**`,
`prompts/**`, `skills/**`, `plugins/**`, `benchmarks/quarantine/**`.

Two mutable files are extra-guarded because they are the judge and the boss:
`config/ruler.toml` and `config/governor.toml` only accept a mutation through
`improve/meta.py::meta_check`, which returns `allowed`/`quarantined`/`blocked`
and demands a green sealed exam **plus** an explicit human ack. The autopilot
alone never applies a change to either. Sealing an exam (quarantine → sealed) is
also a human act: `harness seal <name> --yes`.

## Fail-open vs fail-closed

The rule is not stylistic, it is about who pays for the mistake:

- **Fail-closed** — anything that could write, judge, or grant permission.
  Genome check before touching disk; `meta_check` before touching judge or
  governor; the sealed exam returning False when it finds no units or raises;
  `propose` returning `None` instead of a half-built artifact; the webhook
  refusing with 403 when no token is configured; topology/workflow specs
  rejected without touching disk when they do not compile. A door that opens on
  error is not generous, it is unlocked.
- **Fail-open** — anything that only *informs*. `load_policy` (graph),
  `load_gov` (governor), and `config/ruler.toml` degrade field by field to the
  frozen defaults in code; MCP tool loading degrades to `[]`; episodic memory
  degrades to no recall; `harness report` degrades each section to "(no data)".
  Memory that crashes the run is worth less than no memory, and a report that
  fails on a missing file never gets read.

When in doubt: does the failure mode let something be written or approved that
otherwise would not be? Then fail-closed.

## Canonical commands

```sh
uv sync --extra deepagents                                        # setup
uv run --extra deepagents pytest -q                               # suite (green: 726 passed, 2 deselected)
uv run harness doctor                                             # 17 sanity checks
uv run harness run --unit tests/fixtures/echo --backend mock      # E2E, $0
uv run harness actions                                            # actions + KEEP/DISCARD tally
uv run harness lineage --file --db --limit 20                     # mutation tree + verdicts
uv run harness report --since 24                                  # what the loop did
```

Cheap real E2E: `--backend deepagents --model openai:qwen3.5-9b-mlx` (LM Studio
on :1234 — `lms server start && lms load qwen3.5-9b-mlx`). Project work
goes through `harness queue --project <name>` (use `--no-move` for a dry run),
never through `harness run` alone — `run` is inline and does not enter a
worktree, so it cannot deliver a branch.

## Conventions

- **English**, terse. Docs, comments and commit messages alike. (The Portuguese
  strings still present in CLI output and in module docstrings are being
  migrated; do not add new ones, and do not mass-rewrite them either — that is a
  separate change with its own test churn.)
- Comment only for a non-obvious constraint (the *why*); never paraphrase code.
- Writes into a mutable zone are **atomic** and go through the genome check
  BEFORE touching disk — follow the pattern in `improve/research.py`.
- A new action follows the `research`/`codegen` shape: `propose_*` validates
  everything and writes nothing; `apply_*` does `check` → write atomically;
  register it in `improve/target.py::actions()`.
- Skill = `skills/<name>.md`: `---`, TOML (`name`/`kinds`/`description`), `---`,
  markdown body; `kinds ⊆ {code, content, config, refactor, infra}`.
- Pure functions with injected `now`/`sleep_fn`/`rng` wherever time or randomness
  is involved — the clock is never part of the contract, and a test must never
  sleep.
- New capability requires a pain measured in the ledger. Adopt over reinvent.

## Known traps

- **LangSmith is vetoed.** Tracing off at bootstrap,
  `LANGGRAPH_STRICT_MSGPACK=true`. Do not reintroduce it.
- **The human note is human.** An agent never writes one; >= 3 notes before it
  counts as a KPI.
- **Legacy history is not a prior.** `legacy/results.tsv` has no backend/kind
  columns and would poison the new Wilson prior.
- **Verify is the exit code.** The verdict is the exit code of the verify
  command, never the agent's word — "it passed" without running does not exist.
- **`harness init` rewrites `config/projects.toml` from scratch** and drops its
  header comments. Restore them by hand after re-running init.
- **`lineage.jsonl` contains DISCARDs.** The append happens before the verdict;
  join with the `mutations` table (`harness lineage --db`) to read outcomes.
- **`--max-turns` does not apply in project mode**; the graph uses
  `config/graph.toml` plus the governor. The CLI warns on stderr instead of
  half-obeying — keep that habit: say it out loud rather than silently ignoring
  a flag.

## Before saying "done"

Re-read the diff. Run the suite and `harness doctor`. If something fails, report
the failure — do not report "passed". A green claim without pasted output is
worth less than an honest red one. Open gaps belong in `STATUS.md`, not in a
commit message that nobody re-reads.
