# STATUS — source of truth for harness-core

**Updated:** 2026-08-05. Technical north: `docs/SPEC-rebuild.md` (core rebuild)
and `docs/SPEC-MULTIPROJECT.md` (real projects). Real library APIs:
`docs/RESEARCH-deepagents-api.md`. Architecture detail: `docs/ARCHITECTURE.md`.

This file says what is load-bearing, what is a first cut, and what is known to
be broken or unfinished. It is not a changelog and not a roadmap.

## What the repo is

The `harness/` package (vendor-agnostic core) on LangGraph runs units of work
through a graph (`plan → route → provision → execute → verify → measure → gate →
record`), measures KPIs against a frozen baseline, and evolves itself through 14
registered actions inside the genome's mutable zones, judged by a ruler it
cannot touch. Source of truth for runs: `data/runs.sqlite` (TSV is an export).
The legacy tree in `legacy/` is frozen read-only reference, excluded from pytest
and from the genome. Private projects live under `projects/` but are gitignored —
the public repo carries only fixtures and synthetic benchmarks.

## Verified numbers

Measured on this machine, on the current tree:

- `uv run --extra deepagents pytest -q` → **1606 passed, 1 skipped, 5 deselected**
  (55s).
- `uv run harness doctor` → **22 checks, 0 failures, 1 warning** (local MLX
  server not up — expected when off-grid fallback isn't running).
- `uv run harness actions` → **15 registered actions**.
- `uv run harness --help` → **36 subcommands**.
- The executor exposes **25 harness tools** on top of deepagents' filesystem
  tools (enumerated per family in `README.md`).
- `uv run harness frontier` → **5 open exams** (quarantine candidates the
  current harness fails).
- `uv run harness bench provision --n 10` → p50 ~0.07–0.09s (git worktree,
  versus 50–200s for the legacy copy-based provisioner).
- Real kill-9 resume test passes (`tests/test_resume.py`).
- The improvement loop has run end to end against a local LM Studio/MLX model at
  **$0**.
- **First real end-to-end delivery**: the bancada site, 5/5 units accepted and
  integrated into `~/projects/bancada-app` master via the `claude_code` backend,
  total cost **$3.63** (cap $5/run).
- **Blind lay-tester run**: 2/2 units accepted, **$0.50**, zero flags, verdict
  "a layperson can use it".
- **Skills marketplace** shipped: `harness market sync|search|install|approve`,
  human-gated activation, smoke-tested with 17 Anthropic skills.
- **Off-grid fallback**: Anthropic primary, degrades to a local model with a
  ledger stamp when the primary is unavailable.
- **`PROGRESS.md` retired** in favor of beads (`bd ready`) for pending work.

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
- **Topology per kind**: an optional `[kinds.<kind>]` section in
  `config/topology.toml` replaces the *whole* graph for units of that kind
  (`harness/graph/by_kind.py`). Never a partial merge (section with only `nodes`
  or only `edges` is a fail-closed `TopologyError`); fail-open only at
  `build_for_unit`, which degrades to the default graph with one stderr line.
- **reflect**: the checker of the retry path (`harness/graph/reflect.py`),
  deterministic and $0. The hint it injects into the next attempt is
  **structural** by design — file list, KPI specs, exit reason — and never
  carries the verifier's log text.
- **ruler**: wilson, kpi, verify (exit code is the verdict; log written to
  `$HARNESS_DATA_DIR/logs/<run_id>/`, outside the workspace), note, gate.
  Knobs in `config/ruler.toml`, each falling back to a frozen default.
- **genome + tamper**: 6 immutable glob patterns (`harness/ruler/**`,
  `harness/genome/**`, `harness/routing/**`, `harness/graph/**`, `uv.lock`,
  `benchmarks/sealed/**`), fail-closed check before any write, fingerprint
  compared at the gate.
- **routing**: orthogonal kinds plus a Wilson prior keyed on
  `(kind, tier, backend)`; tier escalation per attempt.
- **governor**: run/cycle deadlines, `cost_cap_usd`, `turn_taper`, explore
  budget and action bench with expiry. Pure functions, injected clock,
  fail-open per field; mutating it requires the meta-exam.
- **autopilot + 14 actions** (`research`, `procedural`, `decompose`, `codegen`,
  `synthesize`, `redteam`, `topology`, `topology_kind`, `workflow`, `evolve`,
  `skill_prune`, `prompt`, `dream`, `node`), all writing atomically and only
  after the genome check.
- **procedural**: skills mined from the tool traces of *accepted* runs
  (`data/logs/<run_id>/trace.<attempt>.jsonl`), selected by **lift** over the
  failed corpus rather than raw frequency — an n-gram equally common in both
  teaches nothing and would just compete for prompt space.
- **structural operators for `topology_kind`** (`insert_node`, `remove_node`,
  `rewire_edge`, `split_parallel`), checked against
  `improve/topology_grammar.py` before the A/B: `split_parallel` only emits a
  closed diamond whose branches are events-only nodes, so a branch that writes
  state never runs in parallel.
- **dream**: offline consolidation of episodic memory (no LLM, no network) —
  fuses recurrent traces into at most one candidate skill, soft-archives aged-out
  orphans, writes the report to `data/dreams/`. `scripts/evolve.sh` gates it on
  `should_dream`, so the loop only sleeps when there is sleep debt.
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
  git repos, one worktree and one branch per unit; `harness queue --integrate`
  (on by default) merges each accepted delivery into the default branch so the
  next unit of the progressive queue sees it.
- **triggers**: JSON inbox with `done/`/`bad/` quarantine, ledger and inbox
  pollers, and a fail-closed HTTP webhook (403 without a token, per-IP rate
  limit, body ceiling).
- **report**: `harness report` joins runs, mutations, skill usage and lineage
  into markdown; every section is fail-open and degrades to "(no data)".
- **doctor**: 21 checks including the evolution surfaces (skills, topology,
  actions, ruler, mcp, lineage, executor, plugin nodes) and the local model
  server.
- **executor tools**: 25 tools in 11 families (files, web, flows, servers, view,
  dom, review, symbols, repo map, scaffold, blocker), each with its own fence,
  plus `SmartFilesystemMiddleware` replacing deepagents' own filesystem tools by
  `.name`. Listed with their guarantees in `README.md`.
- **read-before-write**: `backends/smart_fs.py` refuses a write whose read sha256
  no longer matches disk, and refuses a rewrite under 70% of the current size
  (shrink-guard). The refusal message tells the model to read again rather than
  arguing with it.
- **network fence**: `backends/ssrf.py` is fail-closed — checks every resolved
  address, re-checks on every redirect hop, allows 80/443 only unless a port is
  opted in, and a missing or broken `config/web.toml` means no web tool at all.
- **trust boundary**: `harness/trust_boundary.py` wraps loop-generated content
  (skill bodies, failure traces, checker hints, recalled human decisions) in
  `<untrusted_reference_data>` with the tags neutralised inside the body; the
  system prompt keeps only the skill index. `HARNESS_TRUST_BOUNDARY=0` is the
  kill switch. `harness/redact.py` scrubs secrets on every logging path.
- **human decision memory**: `harness/memory/decisions.py`, an FTS5 sibling of
  the episodic index in the same `data/runs.sqlite`, labelled "a human said this
  before (not an order)". `HARNESS_DECISIONS=0` is the kill switch; no FTS5 build
  makes the whole API a silent no-op.
- **deterministic compaction**: `ContextEditingMiddleware` clears old tool output
  by rule, never by summarising, and never touches `write_file`/`edit_file`.
- **loop guard + typed blockers**: identical repeated tool calls end the run as
  `exit_reason = "stalled"`; `declare_blocker` ends it as `blocker` with a typed
  reason. `truncated` covers a response that died at the token ceiling. All three
  are honest reds `reflect` can act on.
- **tokens in the ledger**: `tokens_in`/`tokens_out` are their own columns next to
  `cost_usd` (no backfill for old rows) and `harness report` sums them.
- **delta gate**: `config/graph.toml` `delta_gate` (on by default) stops handing
  attempts to a unit whose `[checks]` score is not moving — it escalates instead.
- **ZPD curriculum**: `harness/improve/zpd.py` ranks units by historical graded
  score in the 0.4–0.8 band, averaged over the last K *attempts*. Exposed as
  `harness queue --zpd`, off by default.
- **counterfactual replay**: `harness whatif` re-runs the ledger's past failures
  against today's config without mutating anything.
- **workspace servers**: `start_server`/`local_probe`/`stop_server` register in
  the workspace's `procs.json`, are visible through `harness procs`, and get
  killed on teardown. `harness cache-gc` prunes the uv/npm cache to its ceiling.
- **episodic memory**: FTS5 index over failure traces, namespaced on the
  **global** `HARNESS_DATA_DIR` (not the experiment's db), recalled by the
  deepagents backend per kind, soft archiving in a companion table, and
  `HARNESS_EPISODIC=0` as a kill switch read on every call. The sealed exam and
  the frontier screening run inside `episodic.disabled()`, so a recalled episode
  cannot flatter the judge.

## Experimental (works, thin evidence, expect the shape to change)

- **Pareto gate** (`ruler/pareto.py`): shipped `enabled = false`. Turning it on
  makes a Wilson KEEP that regressed cost or wall time INCONCLUSIVE. Not yet
  exercised by a long run, so the tolerances (10%/10%) are guesses.
- **coevolve / frontier** (POET): the quarantine now holds 5 real exams carved
  out of a workshop project (`u1_esqueleto`, `u3_busca`, `u4_dark_mode`,
  `u5_grafico_svg`, `u6_sobre_e_validacao`) and all 5 are on the frontier. But
  the screening runs candidates with the mock backend by default, so "the harness
  fails this" currently means "the mock path fails this". Useful as a curriculum
  signal, not as a difficulty measurement.
- **redteam**: counter-examples land in quarantine as candidate exams. Whether
  the model actually finds real instruction bugs (rather than restating the
  skill) has not been measured across enough cycles to tell.
- **evolve (PBT + MAP-Elites)**: fitness is the real gate verdict, and the
  archive works, but the population sizes that have actually been run are tiny
  (pop 4, 1 generation). No claim about convergence.
- **plugin nodes** (`harness/graph/plugin_nodes.py` + the `node` action): a
  module under `plugins/nodes/` becomes a graph node only after the AST guard,
  the sealed exam, an explicit `HARNESS_NODE_ACK=1`, and an exact sha256 recorded
  in `data/node_approvals.jsonl`. The machinery and the `doctor` check are in;
  `HARNESS_PLUGIN_NODES` is the kill switch. No node has been approved yet, so
  the path is proven by tests, not by use.
- **ui-verify**: asset and screenshot-size checks are deterministic and cheap;
  `--ask` (a model looking at the screenshot) is opt-in, costs ~$0.01 per call,
  and has no accuracy measurement behind it.
- **visual judge** (`harness/vision.py`, `harness vision-judge`, `view_render`):
  a **local** VLM scores the screenshot 0–10 against a 5-axis rubric. It has run
  for real and returned a usable score on a private frontend project, but that
  project is gitignored, so the number is not reproducible from this repo and no
  figure is quoted here. It is deliberately **not** the binary ruler: it is a
  small-weight subcheck in the graded `[checks]`, and *without vision nobody
  fails* — no `[vision]` block, a mute server, a 500, a non-JSON reply all return
  `unavailable` with `ok=None`. `--ref` (paired comparison in one request) exists
  because absolute scores from a small VLM are unstable, and `--min-nota baseline`
  makes the ruler relative to the last accepted shot.
- **procedural**: the lift filter is principled but the corpus is small — few
  accepted runs with traces means few n-grams clear `MIN_SUPPORT`. No skill from
  this action has been through an A/B yet.
- **decompose**: one backend call returns 2..n sub-units, each validated by
  `add.parse_author_json` (deliberately not a second parallel validator). Proven
  by tests; it has not yet driven a real queue from a large task end to end.
- **repo map / symbols**: the PageRank ranking over the import graph is
  deterministic and cheap, but "the ranking is useful to the model" is an
  assumption — no measurement of whether the ranked map beats an alphabetical one.
- **MCP tools**: `config/mcp.toml` is read and any failure degrades to `[]`.
  Only tested against small local servers.
- **export / import**: the bundle format (skills plus routing prior) has no
  version field yet. Treat it as same-version-only transfer.

## Known gaps

- **No real KEEP from `improve` yet.** The loop proposes, applies, A/Bs and
  reverts correctly, but the first KEEP that lands a mutation for real is waiting
  on authorisation to spend the runs it needs. Everything downstream of a KEEP
  (commit_cfg, attribution, lineage verdict) is therefore proven by tests and by
  mock runs, not by a landed change. `harness actions` shows `KEEP=0 DISCARD=0`
  across all 14 actions, and that is the honest reading.
- **The workshop queue is paused at `u5`/`u6` by order, not by failure.** Those
  two frontier exams are not stuck on a bug; the queue was stopped deliberately.
  Do not read their presence on the frontier as a measured difficulty.
- **The trust boundary's screening path uses a mock.** The wrapping, the tag
  neutralisation and the labelling are real and tested, but the tests that check
  "injected text does not change behaviour" drive a mock executor. Whether a real
  model honours the block under pressure has not been measured.
- **`consecutive-user-messages` is untested against `anthropic:*`.** The
  prompt-assembly path that can emit two user messages in a row has only been
  exercised against the local OpenAI-compatible server. Anthropic's API rejects
  that shape, so an `anthropic:*` model may fail where the local one passes.
- **`SummarizationMiddleware` is deliberately passed over.** Context is trimmed by
  `ContextEditingMiddleware` (rule-based clearing of old tool output) instead. A
  summariser puts a model between the run and its own history: it can silently
  rewrite what happened, and then the ledger is measuring a summary. This is a
  choice, not an omission — do not "fix" it by adding the summariser back.
- **`u4_dark_mode` is an open model frontier, not a harness bug.** The local
  model does not land it; the unit is well formed and its verify is
  deterministic. It stays in quarantine as the honest marker of where the
  current executor ends, and closing it means a better model or a better skill,
  not a change to the ruler.
- **`split_parallel` has never produced a live diamond.** The operator and the
  grammar are done, but a closed diamond requires events-only branches, and the
  only source of those is an approved plugin node — of which there are zero. The
  operator is exercised by tests only.
- **The frontier screening still runs on `mock`.** Same root cause as the sealed
  exam gap below: the 5 quarantine exams are real, but "the harness fails this"
  is measured through the mock path, so the frontier ranks candidates by
  plumbing, not by difficulty.
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
- **Self-approve auto-activation is narrow by design.** It needs all of: a pin
  in `[selfapprove.pinned]`, `--runner real` with LM Studio up for *every*
  case (a single fallback queues instead of activating), and a bundle with
  >= 4 cases / >= 1 holdout case. Today only `skills/python-fixes` (4 cases)
  can reach it; `config/workflows/hotfix` (3 cases) always queues. Nothing is
  pinned out of the box, so a fresh install auto-activates nothing until a
  human pins.
- **External skills are refused by both self-approve branches, not waived.**
  `SkillTunable.write` re-renders through `research.render_skill` and drops
  unknown frontmatter (`origin`, `origin_sha256`, `approved`), so
  `frontmatter-loss` queues the auto path and refuses the human path. Skills
  already laundered by a previous `SkillTunable.write` classify as internal —
  pre-existing state this change does not repair. `kinds` narrowing
  (`kinds[0]` kept, the rest dropped) is the same pre-existing bug, now
  surfaced as `kinds-narrowed`.
- **Self-approve measures only the frozen-eval delta.** `task_value_usd` and
  real-effect R2/R3 evidence are still absent; every stamp says so
  (`before=`/`after=` against the frozen bundle, nothing about downstream
  impact).
- **`min_holdout_cases = 1` is weak generalization evidence.** Raise it to 2
  once any bundle has >= 8 cases. `evals/**` sits outside the genome and
  `smart_fs` enforces no genome rule over it — the bundle pin in
  `[selfapprove.pinned]` is what compensates, not the genome.

## Rules that do not change

- The ruler never sits in the mutable genome; the loop calibrates `config/*.toml`
  only.
- LangSmith is **vetoed**: tracing off at bootstrap, `LANGGRAPH_STRICT_MSGPACK=true`.
- The human note is human. An agent never writes one, and it takes >= 3 notes
  to count as a KPI.
- Adopt over reinvent: a new capability needs a pain measured in the ledger.
- Sealing an exam (quarantine → sealed) is a human act.
