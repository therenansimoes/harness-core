# STATUS — source of truth for harness-core

**Updated:** 2026-08-03. Technical north: `docs/SPEC-rebuild.md` (core rebuild)
and `docs/SPEC-MULTIPROJECT.md` (real projects). Real library APIs:
`docs/RESEARCH-deepagents-api.md`. Architecture detail: `docs/ARCHITECTURE.md`.

This file says what is load-bearing, what is a first cut, and what is known to
be broken or unfinished. It is not a changelog and not a roadmap.

## What the repo is

The `harness/` package (vendor-agnostic core) on LangGraph runs units of work
through a graph (`plan → route → provision → execute → verify → measure → gate →
record`), measures KPIs against a frozen baseline, and evolves itself through 9
registered actions inside the genome's mutable zones, judged by a ruler it
cannot touch. Source of truth for runs: `data/runs.sqlite` (TSV is an export).
The legacy tree in `legacy/` is frozen read-only reference, excluded from pytest
and from the genome. Private projects live under `projects/` but are gitignored —
the public repo carries only fixtures and synthetic benchmarks.

## Verified numbers

Measured on this machine, on the current tree:

- `uv run --extra deepagents pytest -q` → **726 passed, 2 deselected** (21s).
- `uv run harness doctor` → **17 checks, 0 failures, 0 warnings**.
- `uv run harness bench provision --n 10` → p50 ~0.07–0.09s (git worktree,
  versus 50–200s for the legacy copy-based provisioner).
- Real kill-9 resume test passes (`tests/test_resume.py`).
- The improvement loop has run end to end against a local LM Studio/MLX model at
  **$0**.

No benchmark score is claimed. Nothing here has been run against SWE-bench or
any public suite, and no such number should be added to the docs without the
command and output pasted next to it.

## Live (load-bearing, covered by tests, used by the loop)

- **run_graph** with real ruler nodes: `provision` freezes KPI specs, BEFORE
  values and the genome tamper fingerprint; `measure` collects AFTER with those
  frozen specs; `gate` calls `ruler/gate.py` (tamper → revert, red verify →
  retry, KPI regression → revert, else accept); retry becomes `escalate_human`
  at the attempt ceiling. Policy in `config/graph.toml`, fail-open per field.
- **Declared topology and workflows**: `config/topology.toml` plus
  `config/workflows/{hotfix,deep}.toml`, validated fail-closed against the
  `NODE_IMPLS` whitelist; any failure falls back to the built-in topology with
  one stderr line.
- **ruler**: wilson, kpi, verify (exit code is the verdict; log written to
  `$HARNESS_DATA_DIR/logs/<run_id>/`, outside the workspace), note, gate.
  Knobs in `config/ruler.toml`, each falling back to a frozen default.
- **genome + tamper**: 33 immutable patterns, fail-closed check before any
  write, fingerprint compared at the gate.
- **routing**: orthogonal kinds plus a Wilson prior keyed on
  `(kind, tier, backend)`; tier escalation per attempt.
- **governor**: run/cycle deadlines, `cost_cap_usd`, `turn_taper`, explore
  budget and action bench with expiry. Pure functions, injected clock,
  fail-open per field; mutating it requires the meta-exam.
- **autopilot + 9 actions** (`research`, `codegen`, `synthesize`, `redteam`,
  `topology`, `workflow`, `evolve`, `skill_prune`, `prompt`), all writing
  atomically and only after the genome check.
- **bandit policy** over KEEP rate keyed on `(kind, action)`, deterministic,
  consuming the governor's explore budget and bench list.
- **sealed exam**: `improve/exam.py` discovers `benchmarks/sealed/*/unit.toml`
  and runs each through `run_unit`; fail-closed (no units, or any exception, is
  False). Executor configured in `config/ruler.toml` `[exam]`, default `mock`.
- **meta_check**: changing the judge (`config/ruler.toml`) or the boss
  (`config/governor.toml`) returns `allowed`/`quarantined`/`blocked` and needs a
  green sealed exam plus an explicit human ack.
- **skills**: load/select/render per kind, injected into the deepagents system
  prompt; `skill_usage` table plus Wilson-lower-bound lift and pruning to
  `skills/attic/`.
- **lineage**: `data/lineage.jsonl` plus `harness lineage`, tolerant to a
  malformed line, orphan becomes root, enriched with verdicts from `mutations`.
- **real projects**: `harness init` / `add` / `queue` / `status` against actual
  git repos, one worktree and one branch per unit.
- **triggers**: JSON inbox with `done/`/`bad/` quarantine, ledger and inbox
  pollers, and a fail-closed HTTP webhook (403 without a token, per-IP rate
  limit, body ceiling).
- **report**: `harness report` joins runs, mutations, skill usage and lineage
  into markdown; every section is fail-open and degrades to "(no data)".
- **doctor**: 17 checks including the evolution surfaces (skills, topology,
  actions, ruler, mcp, lineage, executor).

## Experimental (works, thin evidence, expect the shape to change)

- **Pareto gate** (`ruler/pareto.py`): shipped `enabled = false`. Turning it on
  makes a Wilson KEEP that regressed cost or wall time INCONCLUSIVE. Not yet
  exercised by a long run, so the tolerances (10%/10%) are guesses.
- **coevolve / frontier** (POET): the frontier screening runs candidates with
  the mock backend by default, so "the harness fails this" currently means "the
  mock path fails this". Useful as a curriculum signal, not as a difficulty
  measurement.
- **redteam**: counter-examples land in quarantine as candidate exams. Whether
  the model actually finds real instruction bugs (rather than restating the
  skill) has not been measured across enough cycles to tell.
- **evolve (PBT + MAP-Elites)**: fitness is the real gate verdict, and the
  archive works, but the population sizes that have actually been run are tiny
  (pop 4, 1 generation). No claim about convergence.
- **episodic memory**: recall is wired into the deepagents system prompt and
  writes happen on failure in `run_graph`. Fail-open everywhere, but see the
  divergence gap below.
- **ui-verify**: asset and screenshot-size checks are deterministic and cheap;
  `--ask` (a model looking at the screenshot) is opt-in, costs ~$0.01 per call,
  and has no accuracy measurement behind it.
- **MCP tools**: `config/mcp.toml` is read and any failure degrades to `[]`.
  Only tested against small local servers.
- **export / import**: the bundle format (skills plus routing prior) has no
  version field yet. Treat it as same-version-only transfer.

## Known gaps

- **Episodic write/recall DB divergence in A/B.** `run_graph` records failures
  into the run's ledger DB, while the deepagents backend recalls from the
  default `$HARNESS_DATA_DIR` DB. When an A/B fans out into a temporary
  `data_dir`, the write and the read hit different databases, so recall silently
  returns nothing for that arm. It never returns *wrong* memories — it returns
  none — but any measurement of "does memory help" inside an A/B is currently
  measuring the empty case.
- **`harness run --max-turns` does not apply in project mode.** A unit with
  `project =` is routed through the graph, which takes its turn ceiling from
  `config/graph.toml` plus the governor. The CLI prints a warning on stderr
  rather than silently half-obeying; `--repo` is ignored there for the same
  reason (the workspace is the registered repo's worktree).
- **`task_s01` / `task_s02` are waiting on human review.** They sit in
  `benchmarks/sealed/` without a `unit.toml`, so the exam does not discover
  them. Promoting them into the exam is a deliberate human act, and until
  someone reviews them the sealed exam consists of `sealed_s01`/`sealed_s02`
  only — two deterministic mock units, which is a smoke test, not a hard exam.
- **The default sealed exam runs on `mock`.** `[exam] backend = "mock"` proves
  the graph runs end to end for $0; it does not prove a model can do the task.
  Codegen mutations are therefore judged by a weaker exam than the one the
  design assumes. Pointing `[exam]` at a real backend requires the meta-exam.
- **DISCARD also appears in `lineage.jsonl`.** The lineage append happens before
  the verdict, so the tree contains rejected mutations. Reading it as "what was
  kept" is wrong; join with the `mutations` table for verdicts (`harness lineage
  --db` does this).
- **Skill attribution joins on `session_id` when `run_id` is missing.** A
  divergent id dilutes the measured lift; it never inflates it.
- **No mutation→action mapping in the `harness actions` tally.**
  `MutationRow` carries only `rule_id`, and the action name travels in the
  `note`, so the KEEP/DISCARD scoreboard is reconstructed from text.
- **No lineage for prompt mutations.** `improve/prompt_evolve.py` keeps
  `before_text` for a byte-exact revert but writes no lineage entry.
- **`harness init` rewrites `config/projects.toml` from scratch** and drops the
  header comments (`projects._write`). Re-running init means restoring that
  header by hand.
- **`config/triggers.toml` is not shipped.** The webhook is therefore off by
  default, which is intended, but it also means the HTTP path has no
  out-of-the-box smoke test in the repo.
- **Legacy history is not usable as a prior.** `legacy/results.tsv` has no
  backend/kind columns; feeding it into the new Wilson prior would poison it.

## Rules that do not change

- The ruler never sits in the mutable genome; the loop calibrates `config/*.toml`
  only.
- LangSmith is **vetoed**: tracing off at bootstrap, `LANGGRAPH_STRICT_MSGPACK=true`.
- The human note is human. An agent never writes one, and it takes >= 3 notes
  to count as a KPI.
- Adopt over reinvent: a new capability needs a pain measured in the ledger.
- Sealing an exam (quarantine → sealed) is a human act.
