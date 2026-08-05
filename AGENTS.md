# AGENTS.md — constitution for any AI agent working in this repo

Applies to Claude Code, Cursor, the harness's own autopilot, and humans in a
hurry. Source of truth for state: `STATUS.md`. Source of truth for the genome:
`config/genome.toml`.

## What the repo is

A self-evolving agent harness on LangGraph. The `harness/` package
(vendor-agnostic core; default executor deepagents, optional `claude_code`
backend) runs units of work through a graph (`plan → route → provision → execute
→ verify → measure → gate → record`), measures KPIs against a frozen baseline,
and evolves itself through 14 registered actions (`research`, `procedural`,
`decompose`, `codegen`, `synthesize`, `redteam`, `topology`, `topology_kind`,
`workflow`, `evolve`, `skill_prune`, `prompt`, `dream`, `node`) — always inside
the genome's mutable zones, judged by a ruler it cannot touch. Ledger in
`data/runs.sqlite`; `legacy/` is frozen read-only reference, outside pytest and
outside the genome.

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

## Kill switches

Every optional surface has an env var that turns it off, read on **every call**
so a rollback needs no restart and no data migration. Set to `0`:

| var | turns off | what happens instead |
|---|---|---|
| `HARNESS_TRUST_BOUNDARY` | the `<untrusted_reference_data>` block | previous behaviour: one prompt channel, skill bodies in the system prompt |
| `HARNESS_DECISIONS` | recall of past human decisions (`memory/decisions.py`) | escalation gets no prior-decision block |
| `HARNESS_EPISODIC` | FTS5 recall of past failures | no recall; the sealed exam and frontier screening already run with it off |
| `HARNESS_PLUGIN_NODES` | plugin nodes as graph nodes | the built-in topology only |

`HARNESS_NODE_ACK=1` is the opposite kind of switch — an explicit human ack, so
it defaults to off and setting it to `1` is a decision, not a rollback.

## The executor's protocol

If you are writing or reviewing executor-facing code, these are the invariants:

- **Read before write.** A write is refused when the sha256 of the content the
  model read no longer matches disk (`backends/smart_fs.py`); the refusal tells it
  to read again. A rewrite under 70% of the current size is refused by the
  shrink-guard. Do not add a bypass — "the model knows what it is doing" is the
  exact assumption that truncates files.
- **Instruction and data do not share a channel.** Anything the loop generated or
  collected (skill body, failure trace, checker hint, recalled decision) goes
  through `harness/trust_boundary.py`, never straight into the system prompt. New
  sources of loop-generated text must go through it too.
- **The network is fail-closed.** Web tools go through `backends/ssrf.py`: every
  resolved address, every redirect hop, 80/443 unless opted in. No
  `config/web.toml` means no web tool.
- **Say you are stuck instead of spinning.** `declare_blocker` (typed) and the
  loop guard (`stalled`) exist so a dead end is one honest red instead of a spent
  turn budget. Prefer adding a blocker type over adding retries.
- **Compaction is deterministic.** Old tool output is cleared by rule
  (`ContextEditingMiddleware`), and `write_file`/`edit_file` are excluded.
  `SummarizationMiddleware` is a deliberate no: a model that rewrites the run's
  own history makes the ledger measure a summary. See `STATUS.md`.
- **Secrets never reach a log.** `harness/redact.py` on every path that logs,
  reports or writes a trace.

## Canonical commands

```sh
uv sync --extra deepagents                                        # setup
uv run --extra deepagents pytest -q                               # suite (green: 1274 passed, 1 skipped, 5 deselected)
uv run harness doctor                                             # 21 sanity checks
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

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
