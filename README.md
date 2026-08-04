# harness-core

A **provider-agnostic** agent harness that improves itself with proof. The core
(`harness/`) knows no vendor: execution happens in a pluggable **backend**, and
the default executor is
[deepagents](https://github.com/langchain-ai/deepagents) on
[LangGraph](https://github.com/langchain-ai/langgraph). It runs for free against
a local MLX model served by LM Studio; the same unit of work runs on any registered
backend.

What the harness adds on top of an ordinary agent loop is the **ruler**:
deterministic verify, KPIs measured before and after, one gate that concentrates
the decision, a Wilson verdict on A/B, and a genome that declares what the loop
itself may **not** change. Without that, "the agent got better" is an opinion.

MIT licensed (`LICENSE`). Python >= 3.11.

## Quickstart

```bash
uv tool install --from . "harness-core[deepagents]"   # once
cd your-project
harness quickstart                     # environment check
harness do "fix the bug in target.py"
```

`harness do` decides model, route, plan and verification on its own; advanced
flags live in `harness do --help`. The detailed flow below is the advanced mode.

### Advanced mode (explicit unit.toml)

Local model, $0:

```bash
uv sync --extra deepagents
lms server start                    # LM Studio: OpenAI-compatible API on :1234
lms load qwen3.5-9b-mlx             # 18GB laptop: keep the local model <= 9B (MLX)
```

Registered backends and preflight (deterministic, zero LLM calls):

```console
$ uv run harness backends
claude_code      ok             2.1.220 (Claude Code)
deepagents       ok             deepagents importable
mock             ok             mock always available
```

The third column depends on the machine: without the `deepagents` extra the line
becomes `unavailable`, and the same happens to `claude_code` without the
official CLI installed.

A unit of work is a directory with a `unit.toml` (`id`, `prompt`, `verify_cmd`,
optional `kind`). Running the `tiny_fix` fixture against the local model:

```bash
uv run harness run --unit tests/fixtures/tiny_fix \
  --backend deepagents --model openai:qwen3.5-9b-mlx
```

An `openai:*` model points at LM Studio, not at the cloud: the backend defaults
`OPENAI_BASE_URL` to `http://localhost:1234/v1` (and `OPENAI_API_KEY` to a dummy,
which LM Studio ignores). Set both explicitly to aim somewhere else. The local
MLX models are listed at $0 in the `[pricing]` table of `config/models.toml`,
so the ledger row lands with `cost_usd = 0.0`. That run's output depends on the
model you happen to have installed, which is why it is not pasted here; the
shape is the same as the `mock` example below.

Letting the router choose:

```bash
uv run harness run --unit tests/fixtures/tiny_fix --route auto
```

`--route auto` prints one extra line before the run
(`route auto <unit> kind=… tier=… <backend> <model> [reasons]`) and is mutually
exclusive with `--backend`/`--model` — the router decides. **Cost warning:** in
the shipped config `[router.kind] code = "t1"`, and tier `t1` is `claude_code`
(paid). For a fully local setup, point `code` at `t0` in `config/models.toml` or
pin `--backend deepagents` by hand.

Everything that needs no model runs on the deterministic `mock` backend:

```console
$ uv run harness run --unit tests/fixtures/echo --backend mock
a10bc523d66d echo mock accept verify ok, sem regressão de KPI 0.01s ledger#1

$ uv run harness run --unit tests/fixtures/tiny_fix --backend mock
c103bdb4c96a tiny_fix mock retry verify_failed:exit=1 0.03s ledger#2
```

(`mock` only writes the prompt to a file: `echo` passes, `tiny_fix` fails
verify — that is how the gate proves itself without spending on APIs.)

`harness run` exits 0 when the gate says `accept`, 1 otherwise. Data lives under
`$HARNESS_DATA_DIR` (default `data/`, gitignored): `runs.sqlite` (ledger),
`checkpoints.sqlite`, `ws/` (per-run worktrees), `logs/<run_id>/` (verify logs,
kept outside the workspace so a retry cannot read them as source), `inbox/`
(event triggers), `dreams/` (episodic consolidation reports), `lineage.jsonl`,
`frontier.jsonl`, `node_approvals.jsonl`, `archive.sqlite`.

Other commands:

```console
$ uv run harness ab --a 5/6 --b 6/6
INCONCLUSIVE a=5/6 [0.44,0.97] b=6/6 [0.61,1.00]

$ uv run harness bench provision --n 10
provision n=10 p50=0.069s p95=0.072s

$ uv run harness doctor
… doctor checks=21 falhas=0 avisos=0
```

## Architecture

```
harness/
  cli.py        27 subcommands (table below)
  types.py      UnitSpec ExecRequest ExecResult Selection Verdict RunRow MutationRow
  backends/     base(Protocol) registry(entry point) mock deepagents claude_code auth/
                file_tools smart_fs web_tools ssrf flow_tools procs dom_tools
                review_tools blocker_tools loop_guard safe_shell  <- the executor's tools
  trust_boundary.py  instruction vs data in the prompt (untrusted_reference_data)
  redact.py     secret redaction on every path that logs or reports
  symbols.py repomap.py scaffold.py vision.py   <- code navigation, scaffolding, visual judge
  graph/        run_graph (one run)  autopilot_graph (one improvement cycle)  topology
                by_kind (a whole topology per kind)  reflect (retry's skeptic)
                custom (named workflows)  plugin_nodes (approved plugin nodes)
  ruler/        wilson kpi verify note pareto gate    <- who measures and who decides
  genome/       genome tamper                          <- what may change
  routing/      kinds (WHAT it is)  router (HOW MUCH it may cost)
  governor/     deadline, cost cap, turn taper, explore budget, action bench
  workspace/    provision (git worktree + cache symlink), sealing
  improve/      target policy mutate escalate research codegen meta synthesize
                exam coevolve redteam workflow_action replay lineage
                dream (episodic consolidation) node_action topology_evolve
                procedural (skills mined from tool traces) decompose zpd (curriculum)
  evolve/       population (PBT) + fitness (gate-scored) + archive (MAP-Elites)
  skills/       load/select/render + attribution (per-skill lift)
  memory/       episodic (FTS5 case-based recall of past failures)
                decisions (FTS5 recall of what a human answered when the loop stopped)
  triggers/     inbox (JSON files) + webhook (HTTP, fail-closed) + watch (polling)
  projects.py   real git repos as targets; queue.py drives their queues
  report.py     self-report for humans (runs, mutations, skills, lineage)
  ledger/       store (SQLite; TSV is an export)      <- source of truth for runs
skills/*.md     distilled skills (TOML frontmatter + markdown), loop-mutable
prompts/        executor.md, the evolvable base prompt of the executor
plugins/        the only loop-mutable CODE zone, judged by the sealed exam
                nodes/ is the slot for graph nodes, approved one sha256 at a time
config/*.toml   models kinds tools catalog genome graph mcp topology ruler
                governor projects + workflows/  <- the loop-calibrable zone
benchmarks/     held_in (evaluation units) quarantine (candidates) sealed (the exam)
```

**run_graph** (`harness/graph/run_graph.py`) is the topology of a single run:
`plan → route → provision → execute → verify → measure → gate →
[accept | retry → route | escalate | revert] → record → END`. Checkpointing uses
`SqliteSaver` with `thread_id = run_id` and idempotent nodes: killing the
process mid-`execute` and re-invoking the same thread finishes the run without
executing twice (`tests/test_resume.py` does that with a real `kill -9`). The
nodes are immutable in the genome, but the wiring is data: `config/topology.toml`
declares nodes/edges validated fail-closed against the whitelist in
`harness/graph/topology.py`, and any failure falls back to the built-in topology
with one line on stderr. A `[kinds.<kind>]` section declares a *whole other*
topology for units of that kind (`harness/graph/by_kind.py`): never a partial
merge — the section replaces the graph or does not exist — and fail-open only at
the edge, where anything malformed becomes one stderr line and the default
graph. Named variants live in `config/workflows/*.toml` (shipped: `hotfix`,
`deep`) and are loop-mutable through the `workflow` action.

`reflect` is the checker half of the retry path (`harness/graph/reflect.py`):
deterministic, $0, no LLM. It reads what the ruler charged and the attempt did
not deliver, and writes a **structural** hint (files, KPI specs, exit reason)
into the next attempt's prompt. The verifier's log text never enters that hint —
a small model needs a file list, not the prose of its own failure.

`provision` freezes the baseline (KPI specs and BEFORE values plus the genome
tamper fingerprint, frozen defaults — a run does not redefine its own ruler),
`measure` collects the AFTER with those frozen specs, and `gate` calls the
combiner in `ruler/gate.py`. Run policy lives in `config/graph.toml`
(`max_attempts`, `verify_timeout_s`, per-node toggles), read at runtime with
fail-open defaults. Details in `docs/ARCHITECTURE.md`.

**ruler** (`harness/ruler/`) is the ruler, and it is deliberately one piece.
`verify.py` runs the unit's `verify_cmd` (success is exit 0, never what the
agent claims) and writes the log to `$HARNESS_DATA_DIR/logs/<run_id>/`;
`kpi.py` collects project KPIs using specs read **before** the change;
`wilson.py` gives the interval and the KEEP/DISCARD/INCONCLUSIVE verdict;
`pareto.py` is an opt-in second filter that turns a Wilson KEEP into
INCONCLUSIVE when cost or wall time regressed beyond tolerance; `note.py` holds
the 1–5 human note, the one KPI the harness cannot measure by itself; `gate.py`
combines everything in a single place: tamper → `revert`, red verify → `retry`,
KPI regression → `revert`, otherwise `accept`. Both `cli.run_once` and the
run_graph `gate` node go through that combiner.

The judge's knobs live in `config/ruler.toml`: `[gate].kpi_regression_tolerance`,
`[pareto]` (disabled by default, matching historical behaviour), and `[exam]`
(which backend/model executes the sealed exam; `mock` by default). Any malformed
read falls back to the frozen default, and changing that file goes through
`improve/meta.py::meta_check`, which requires a green sealed exam plus a human
ack (`allowed`/`quarantined`/`blocked`).

**genome** (`harness/genome/` + `config/genome.toml`) separates mutable from
immutable. Immutable: `harness/ruler/**`, `harness/genome/**`,
`harness/routing/**`, `harness/graph/**`, `uv.lock`, `benchmarks/sealed/**`.
Mutable: `config/*.toml`, `config/workflows/**`, `prompts/**`, `skills/**`,
`plugins/**`, `benchmarks/quarantine/**`. `tamper.py` fingerprints before and
compares after, and a violation reaching the gate becomes `revert`; the
autopilot's `genome_check` stays fail-closed BEFORE anything is written.

**router** (`harness/routing/`) splits two questions the old harness conflated:
`kinds.py` classifies **what** the unit is (`code`, `content`, `config`,
`refactor`, `infra`) deterministically, and `router.py` chooses **how much** it
may cost (tier `t0`/`t1`/`t2` from `config/models.toml`). The success prior is
keyed on `(kind, tier, backend)` — a bad history for `(code, t0)` does not
condemn `(content, t0)` — and a Wilson lower bound below `prior_floor` bumps a
tier, as does repeated failure per attempt.

**governor** (`harness/governor/`) is the boss: `config/governor.toml` sets the
run and cycle wall-clock deadlines, the per-run `cost_cap_usd`, the
`turn_taper` that shortens `max_turns` on each attempt, and the focus knobs
(`explore_frac_start`/`end` melt exploration as the deadline approaches;
`bench_after`/`bench_cycles` bench an action that keeps proposing without
landing a KEEP, and return it after the expiry). All pure functions with `now`
injected; every field is fail-open to a frozen default. Mutating
`config/governor.toml` goes through the same meta-exam as the judge — the loop
does not loosen its own deadline.

**autopilot** (`harness/graph/autopilot_graph.py` + `harness/improve/`) is the
improvement loop: `pick_target (the policy picks the action when the caller does
not pin one) → propose → genome_check → apply → fanout_ab → score →
[KEEP: commit_cfg | DISCARD/INCONCLUSIVE: revert_cfg] → attribute → record`. Any
node can divert to `escalate`, which is LangGraph's `interrupt()`: the graph
stops and waits for a human instead of improvising.

## Subcommands

| command | what it does |
|---|---|
| `run` | run one unit with a backend (`--route auto` lets the router pick; `--project` runs in a worktree of the registered repo) |
| `ab` | Wilson verdict between two arms |
| `init` | register a real git repo as a project (writes `config/projects.toml`) |
| `status` | per project: queue/done/stuck plus total ledger spend |
| `queue` | drain a project's queue through the graph, one unit at a time (accept → `queue/done/`, stuck → `queue/stuck/`; accepted branches are merged into the default branch, `--no-integrate` opts out; `--zpd` reorders by learning value, practice queues only) |
| `backends` | list registered backends plus preflight |
| `improve` | improvement cycle: mutate config and test it in A/B |
| `replay` | attribute a historical delta to a mutation |
| `whatif` | re-run the ledger's past failures against today's config — counterfactual, no mutation |
| `lineage` | genealogy tree of code mutations with verdicts |
| `report` | self-report of the loop (runs, mutations, skills, lineage, tokens) |
| `ui-verify` | UI verify: serve the dist, check loadable assets, look at the screenshot |
| `vision-judge` | UI subcheck: a local VLM scores the screenshot 0–10 (`--min-nota baseline`, `--ref` for a paired comparison); fail-open |
| `export` | pack skills plus routing prior into a `.tgz` bundle |
| `import` | bring skills plus prior in from another project's bundle |
| `doctor` | local diagnosis: backends, genome, config, data, tracing, plugin nodes (21 checks) |
| `skills` | list loaded skills (name, kinds, description; `--lift` adds attribution) |
| `actions` | list registry actions plus their KEEP/DISCARD tally from the ledger |
| `procs` | list servers registered in the runs' workspaces (`start_server` writes `procs.json`) |
| `cache-gc` | prune the dependency cache (uv/npm) down to its ceiling |
| `add` | author a unit from a natural-language task (`--ui` appends a `ui-verify` step) |
| `decompose` | break a large task into an ordered queue of atomic sub-units, each validated by `add`'s own parser |
| `seal` | promote a quarantine exam into `benchmarks/sealed` (human act, `--yes`) |
| `frontier` | list quarantine exams the current harness still fails |
| `evolve` | PBT over configs: score each individual at the gate, archive the elites |
| `webhook` | open the loopback HTTP port that drops events into the inbox (fail-closed) |
| `bench` | measure the cost of a harness operation |

## Backends

A backend implements three methods (`harness/backends/base.py`):
`capabilities()`, `preflight()` — deterministic, **zero LLM calls** — and
`execute(ExecRequest) -> ExecResult`. The core knows nothing beyond that.

| backend | what it is | notes |
|---|---|---|
| `mock` | deterministic, writes the prompt to a file | the backend of the test suite; touches no network |
| `deepagents` | default; model via `init_chat_model` (`openai:…` against LM Studio on :1234, or any LangChain provider) | the only file in the repo that imports LangChain, and the import is lazy |
| `claude_code` | subprocess of the official CLI; `resumable=True` via `--resume` | requires the CLI installed and authenticated; costs money |

A third-party backend does not need to touch the core: publish a package that
advertises itself on the `harness.backends` entry point.

```toml
# pyproject.toml of YOUR package
[project.entry-points."harness.backends"]
my_backend = "my_package.backend:MyBackend"
```

`harness.backends.registry` merges installed entry points with the built-ins
(and with `registry.register(name, factory)`, which is what tests use). After
`pip install`, `harness backends` already lists yours.

Auth follows the same pattern on the `harness.auth` entry point (`AuthAdapter`:
`env()` + `check()`). The repo ships only `NullAuth`.

> An OAuth adapter for a third party's subscription client is a **ToS grey area
> and is out of scope for this repo** — only the `harness.auth` slot exists, for
> whoever wants to publish their own, at their own risk.

`config/mcp.toml` declares optional MCP servers (stdio/streamable_http, via
`langchain-mcp-adapters`); their tools become the deepagents backend's `tools=`,
and any failure degrades to an empty list.

## The executor

The backend hands the model **25 harness tools** in 11 families, and replaces
deepagents' own filesystem tools with a guarded version (the `smart fs` row
below, which is a middleware, not part of the 25). Each family carries its own
fence, because a tool that can reach the network or the disk is the actual attack
surface of the loop:

| family | tools | fence |
|---|---|---|
| files | `file_outline` `edit_range` `insert_lines` `append_file` | path resolved under the workspace, atomic write, backup per edit, syntax validation before commit |
| smart fs | replaces deepagents' `read_file`/`write_file`/`edit_file` | **read-before-write**: the write is refused unless the sha256 of what the model read still matches disk, plus a **shrink-guard** that refuses a rewrite under 70% of the current size |
| web | `web_fetch` `web_search` | `backends/ssrf.py`, fail-closed: only public internet, checked on the **resolved addresses** (all of them) and re-checked on **every redirect hop**; ports 80/443 unless opted in; no `config/web.toml` means no web at all |
| flows | `install_deps` `run_tests` `run_lint` `local_screenshot` `detect_stack` | stack detected from lockfiles, argv0 fenced, timeouts, output filtered |
| servers | `start_server` `local_probe` `stop_server` | port allocated per run, registered in the workspace's `procs.json`, probes are loopback-only, `kill_all` on teardown (`harness procs` lists them) |
| view | `view_render` | screenshot plus an optional **local** VLM judge that returns a 0–10 score; no vision model configured means `unavailable`, never a failure |
| dom | `inspect_dom` `a11y_audit` | reads the rendered tree, writes nothing |
| review | `diff_review` | the model reads its own diff before declaring it done |
| symbols | `find_symbol` `find_references` `signature_of` | deterministic index, no LLM |
| repo map | `repo_map` | PageRank over the import graph, so the map is ranked rather than alphabetical |
| scaffold | `scaffold` `asset_gen` | templates only, inside the workspace |
| blocker | `declare_blocker` | typed blocker (`backends/blocker_tools.TYPES`) — the run exits with `exit_reason = "blocker"` and *says why* instead of burning turns |

deepagents' `write_todos` stays available through `TodoListMiddleware`, so a long
unit keeps a plan the harness can read back.

Six middlewares wrap the loop, in order: `SmartFilesystemMiddleware` (the read
gate above), `ModelCallLimitMiddleware` (the turn ceiling, tapered by the
governor), `TodoListMiddleware`, `ContextEditingMiddleware` (**deterministic
compaction** — it clears old tool output by rule, never by summarising, and never
touches `write_file`/`edit_file`), `LoopGuardMiddleware` (identical tool calls in
a row become `exit_reason = "stalled"` rather than a full turn budget spent
spinning) and `ToolRetryMiddleware` (transient tool errors only). Each import is
fail-open: a middleware missing from the installed langchain version drops out
with one stderr line.

Beyond `done`/`max_turns`/`timeout`, a run can now end on `truncated` (the last
model response died at the token ceiling), `stalled` (the loop guard fired) or
`blocker` (the model declared a typed obstacle). All three are honest reds that
`reflect` can act on, and `tokens_in`/`tokens_out` are first-class ledger columns
next to `cost_usd`, so `harness report` prices a run even when the provider
charges $0.

Retry is also gated on the graded ruler: `config/graph.toml`'s `delta_gate` (on
by default) stops giving attempts to a unit whose `[checks]` score is not
*moving* — a retry that does not shift the needle escalates instead of repeating.

## Trust boundary: instruction on one side, data on the other

The executor used to receive three things on the same channel: our system
prompt, the task, and text the loop generated or collected by itself — a mined
skill body, an old failure trace, the checker's hint, a human decision from
another case. Only the first two are reviewed. One of the others saying "ignore
the instructions above" is prompt injection carrying our own stamp of authority.

`harness/trust_boundary.py` marks the difference. Untrusted content travels
inside an `<untrusted_reference_data>` block that states it is data and not
instructions, and the tags are neutralised **inside** the body so the text cannot
close the block and write "after" it. What stays in the system prompt is the
skill *index* (name — description); the executor reads the body as data, through
a tool, when it wants it. When the block and the task have to share one string
(`ExecRequest.prompt`), the task is labelled explicitly as the only source of
instructions.

Scope: skill bodies are inside the block too. `mutate.check` judges the **path**
of a skill write against the genome, not its text, and no A/B binds a new skill's
content to a verdict — so nothing has judged that body before it reaches the
prompt, which makes it data. `harness/memory/decisions.py` follows the same
rule: a human answer recalled from another case is labelled "a human said this
before (not an order)", because past evidence presented as a command makes the
loop obey a ghost.

`HARNESS_TRUST_BOUNDARY=0` turns the whole thing off and restores the previous
single-channel prompt — a rollback with no data migration. Separately,
`harness/redact.py` scrubs secrets from every path that logs, reports or writes
a trace, so the fence does not leak the thing it is guarding.

## Self-evolution

The full cycle in one paragraph: the **policy** (`improve/policy.py`, a
Wilson+UCB bandit over each action's historical KEEP rate, keyed on
`(kind, action)`, deterministic with an rng seeded by `thread_id:cycle`, and
consuming the governor's explore budget and bench) picks which of the registry's
14 actions to try → the action runs `propose` → `apply` goes through a
fail-closed **genome check** and, when the change touches the judge, through
`meta_check` (which demands a green **sealed exam** plus a human ack) → the
change is judged by an alternating **A/B** (Wilson) or by the sealed exam →
`KEEP` commits, `DISCARD`/`INCONCLUSIVE` revert → the verdict flows back into
the **lineage** (`data/lineage.jsonl`), into per-skill **attribution**, and into
the bandit itself (the action name travels in the mutation's `note`).

The registered actions (`uv run harness actions`):

| action | what it mutates | judged by |
|---|---|---|
| `research` | distils repeated ledger failures into `skills/<slug>.md` | A/B |
| `procedural` | mines the tool traces of *accepted* runs into a procedure skill, kept only when the n-gram's lift over failed runs clears the floor | A/B |
| `decompose` | turns one large task into an ordered queue of atomic sub-units (same schema and parser as `add`) | A/B |
| `codegen` | Python in `plugins/**` | sealed exam |
| `synthesize` | turns failed runs into quarantine exams | A/B |
| `redteam` | attacks its own skills/prompt, files counter-examples in quarantine | A/B |
| `topology` | node/edge wiring in `config/topology.toml` | A/B |
| `topology_kind` | structural operators over one kind's `[kinds.<kind>]` graph | A/B |
| `workflow` | named workflows in `config/workflows/*.toml` | A/B |
| `dream` | consolidates episodic memory: merges recurrences, archives orphans | A/B |
| `node` | a new graph node under `plugins/nodes/` | sealed exam + `HARNESS_NODE_ACK=1` |
| `evolve` | knobs in `config/models.toml` via population mutation | A/B |
| `skill_prune` | moves low-lift skills to `skills/attic/` (never deletes) | A/B |
| `prompt` | `prompts/executor.md` (PromptBreeder-lite, 4 deterministic operators) | A/B |

Two of those actions change the shape of the loop rather than a knob.
`topology_kind` mutates the graph of a single kind through structural operators
(`insert_node`, `remove_node`, `rewire_edge`, `split_parallel`) checked against
the grammar in `improve/topology_grammar.py`, so a bad rewrite is rejected before
the A/B ever runs: `split_parallel` in particular only produces a closed diamond
of **events-only** nodes, which today means it waits for the first approved
plugin node — a branch that writes state does not get to run in parallel.
`dream` is the offline half: no LLM, no network, it reads the episodic index,
fuses the recurrent traces into at most one candidate skill per session, soft-
archives the one-off episodes that aged out, and writes the report to
`data/dreams/`. `scripts/evolve.sh` calls `should_dream` before `improve`, so the
loop only sleeps when there is sleep debt.

Config mutations are not invented from scratch: they come from a declared
catalog (`config/catalog.toml`). Each `[[rule]]` is a falsifiable hypothesis
about one knob — target file, key, `from`, `to`, and the `fails_on` that binds
the rule to real ledger `exit_reason`s. `improve/target.py` orders by expected
gain (`freq(failure) × mean cost × prior`) and `improve/mutate.py` applies.

```console
$ uv run harness improve --cycles 1 --backend mock --unit tests/fixtures/echo
ciclo0 max_attempts_3_to_4 router.max_attempts 3->4 INCONCLUSIVE a=6/6 b=6/6 delta=+0.00 revertida mut=f4dff79cc02c
improve ciclos=1 mutações=1 intervenções=0 intervention_rate=0.00 (n=15)
```

Beyond the per-mutation loop there are three longer arcs:

- **Exam and curriculum.** `improve/exam.py` runs `benchmarks/sealed/*/unit.toml`
  through `run_unit` and returns pass/fail per unit, fail-closed (no
  discoverable units, or any exception, means False). `improve/coevolve.py`
  (POET-style) screens `benchmarks/quarantine/` and reports the *frontier* —
  the candidates today's harness still fails — into `data/frontier.jsonl`;
  `harness frontier` prints it (currently 5 real exams carved out of a workshop
  project: `u1_esqueleto`, `u3_busca`, `u4_dark_mode`, `u5_grafico_svg`,
  `u6_sobre_e_validacao`). Sealing a candidate stays a human act
  (`harness seal <name> --yes`).
- **Population evolution.** `harness evolve` runs PBT over config individuals
  where fitness is the gate's own verdict (`RunRow.ok`, the same thing the A/B
  counts), selects by Wilson lower bound with 25% elitism, and archives elites
  into MAP-Elites niches `(kind, cost_bucket)` in `data/archive.sqlite`.
- **Memory and transfer.** `harness/memory/episodic.py` keeps an FTS5 index of
  past failure traces in the ledger DB; the deepagents backend recalls the top
  matches for the unit's kind and prepends them to the system prompt (fully
  fail-open: no FTS5 build means a silent no-op). `HARNESS_EPISODIC=0` is the
  kill switch, read on every call — the sealed exam and the frontier screening
  run with memory off, so a recalled episode cannot flatter the judge.
  `harness export` / `harness import` move skills plus the routing prior between
  projects as a `.tgz`.

## Real projects

The harness is not limited to fixtures. `harness init <repo> --name <name>`
registers a real git repo in `config/projects.toml` (optional `build_cmd`,
`verify_default`, `queue_dir`); `harness add "<task>" --project <name>` authors a
`unit.toml` from a natural-language task using the repo's real context
(README/package.json/tree), with `--ui` appending
`harness ui-verify dist --expect-asset css` to the authored `verify_cmd`; and
`harness queue --project <name>` drains the queue through the graph, one unit at
a time, each in its own git worktree on an ephemeral branch. The queue is
progressive: a unit that does not accept **stops** the loop, because the next one
depends on it. Accepted work is delivered as branch `harness/<unit_id>` for human
review and the unit moves to `queue/done/`; a stuck unit moves to `queue/stuck/`.
The accepted branch is then merged into the repo's default branch, because the
queue is progressive and the next unit has to see the previous one;
`--no-integrate` turns that off and each unit starts from `HEAD` blind to its
predecessor.
`harness status` counts those buckets plus total spend per project. `--no-move`
is a dry run (it executes and touches nothing in the queue), and
`scripts/queue_run.py` is now a thin env-var wrapper over the same driver
(`harness/queue.py`).

For frontend work, `harness ui-verify dist` serves the built directory, requires
at least one *loadable* asset of each declared kind, and takes a screenshot with
a minimum byte size (a blank page measures ~11kb, a page with content ~28kb);
`--ask` is an opt-in that spends ~$0.01 to have a model look at the shot and
return `{"ok","motivo"}`.

## Triggers

The harness can wake up on an event instead of a command. `data/inbox/*.json`
is the universal door — anything that can write a file (git hook, CI, human,
MCP) drops a JSON there and `triggers/inbox.py` dispatches it to injected
handlers; processed events go to `done/`, malformed ones to `bad/`, and the
processor never crashes on a bad event. `triggers/watch.py` polls the inbox or
the ledger (firing when recent failures cross a threshold).

`triggers/webhook.py` is the HTTP door, and unlike the rest of the config it
fails **closed**: with no token configured it refuses everything with 403. It
reads `[webhook]` from `config/triggers.toml` (not shipped — absent means off):
`token`, `rate_limit`, `rate_window_s`, `max_body_bytes`. `HARNESS_WEBHOOK_TOKEN`
overrides the token so a deployment need not write the secret to a file.

## What is proven and what is not

`1274 passed, 1 skipped, 5 deselected` on `uv run --extra deepagents pytest -q`;
`uv run harness doctor` reports 21 checks, 0 failures, 0 warnings; the loop has
been run end to end against a local LM Studio/MLX model at $0. No benchmark
numbers are claimed here, because none have been measured on a public suite.

Current state, live vs experimental surfaces, and known gaps: `STATUS.md`.
Shortest path from clone to a reviewable branch on your own repo:
`docs/FAST_START.md`. Conventions for agents and contributors: `AGENTS.md` and
`CONTRIBUTING.md`. Design detail: `docs/ARCHITECTURE.md`.
