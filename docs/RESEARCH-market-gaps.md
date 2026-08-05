# Market gaps — harness-core vs. state of the art (deep research, 2026-08-05)

Source: multi-agent deep-research run (6 angles, 25 sources fetched, 122 claims
extracted, 25 adversarially verified by 3-voter panels — 17 confirmed, 8 killed,
108 agent calls, ~2.9M tokens). Research question: what is missing in a
self-evolving agent-orchestration harness built on a compiled state-graph plus a
fleet of role subagents, and what would make it market-grade in mid-2026.

Every claim below carries its verification vote. Claims that did **not** survive
verification are in "Refuted claims" and are as load-bearing as the confirmed
ones: they say what *not* to build.

---

## Summary — the four gap clusters

1. **Safety/autonomy.** The biggest market-grade gap vs. Claude Code is
   OS-enforced sandboxing (Seatbelt/bubblewrap) that replaces per-command
   approval with an enforced boundary, plus the governance layer around it
   (permission gates, budget control, rollback, audit trace).
2. **Optimization target.** The frozen-eval tune loop is scoped to
   skills/prompts/configs and misses the highest-leverage target — **graph
   structure** — although full dynamic rewriting is *not* required for
   repetitive verified coding work; explicit guards matter more than richer
   rewriting.
3. **Evaluation rigor.** A single validation score is insufficient: the tune
   loop needs held-out/OOD views, replay diagnostics, and coding-coupled
   objectives (test pass rate), because harness-level self-editing is both the
   only mechanism shown to lift validation+ID+OOD together and the riskiest.
4. **Table-stakes infrastructure.** First-class pluggable checkpointing and
   linked per-agent + orchestrator traces are the 2026 baseline; ad-hoc logging
   and hardcoded saves do not qualify.

Meta-finding: harness design *itself* is a proven performance lever (SWE-agent's
ACI ablation), which validates the project category — but claims that
self-evolution is a market differentiator, or that the field is converging on
harness-core's exact architecture, were **refuted**.

---

## Findings

### F1 — OS-level sandboxing + governance layer is the largest market gap

**Confidence: high** (votes 3-0, 3-0, 3-0; three claims merged)

Claude Code ships OS-enforced sandboxing (Seatbelt on macOS, bubblewrap+socat on
Linux/WSL2) that bounds filesystem and network *for every Bash command and its
child processes*; in auto-allow mode this replaces per-command human approval
entirely (default writes confined to cwd + session temp dir). The 2026
harness-design survey formalizes this as one of six required runtime components:
a verification-and-governance layer with sandbox execution, permission gates,
human approval, budget control, rollback and audit traces.

- Sources: <https://code.claude.com/docs/en/sandboxing>,
  <https://arxiv.org/pdf/2606.20683>
- Caveat from the docs: `dangerouslyDisableSandbox` and a dependency-missing
  fallback exist, so "every command" is the guarantee *of the sandboxed path*,
  not unconditional.

**Implication for harness-core.** The only boundary today is a shell denylist
(`harness/backends/safe_shell.py`) plus git-worktree workspace isolation
(`harness/workspace/provision.py`). That is a *policy* filter parsing argv, not
an OS-enforced boundary — a shell escape, a Python subprocess, or an unparsed
construct walks straight past it. This is the single item blocking unattended
autonomous runs on a real machine, and it is what would let auto-allow replace
approval prompts instead of adding them.

### F2 — The tune loop's blind spot is graph structure

**Confidence: high** (votes 3-0 ×4; four claims merged)

When failures are structural (missing verification, redundant communication,
insufficient decomposition, wrong control flow), graph-level optimization usually
beats prompt tuning. Both halves are empirically necessary: removing semantic
evolution costs 10.7% and removing topological evolution costs 7.3% on HotpotQA
(HiVA, AAAI 2026 — peer-reviewed). Localized typed graph edits
(`REWRITE_NODE`/`PRUNE_EDGE`/`ADD_NODE`/`ADD_EDGE` over role/logic/tool nodes)
lifted GPT-4.1 ReAct on MCP-Universe from 30.96% to 38.82% in 5 iterations (TPG).
HiVA's co-evolved topology also reached 94.2% HumanEval / 92.1% MBPP and the best
cost-efficiency on GAIA — i.e. self-organized topology improves accuracy *and*
cost together, the same trade-off harness-core's governor targets with
hand-written heuristics.

- Sources: <https://arxiv.org/pdf/2603.22386>,
  <https://arxiv.org/html/2604.20714>, <https://arxiv.org/pdf/2509.00189>
- Caveats: the HumanEval score is a 50-of-164 subset; the GAIA cost-efficiency
  margin over MaaS is small (5.5 vs 5.2).

**Implication for harness-core.** The pieces exist but are not connected. There
*is* a structural mutation action (`harness/improve/topology_evolve.py`, four
deterministic operators) guarded by a real grammar
(`harness/improve/topology_grammar.py`). What is missing is the *measurement*:
`tunable_for` in `harness/improve/tunable.py` only dispatches on `skills/` and
`config/workflows/` prefixes, so topology never gets a frozen eval bundle, a
triple baseline, or a monotonic gate. Topology mutations today are judged by A/B
on the ledger, not by a sealed exam.

### F3 — Dynamic graph rewriting is not the gap; guards on it are

**Confidence: high** (votes 3-0, 3-0; two claims merged)

Static, well-searched workflow templates often dominate ad-hoc runtime graph
generation when the operator space is constrained, the evaluator is trustworthy,
and the workload is repetitive — a condition the survey scopes *explicitly* to
"code generation with unit tests". Moving to in-execution graph editing requires
token/tool/wall-clock budgets, verifier-tied stopping rules, tool-failure
fallback, and loop avoidance (repeated-state detection, edit caps) — treated as
part of the workflow policy itself, not as optional engineering detail.

- Source: <https://arxiv.org/pdf/2603.22386> (Sections 7 and 7.1); independent
  corroboration found for the guards requirement (Agentproof,
  scheduler-theoretic frameworks).

**Implication for harness-core.** Do *not* invest in richer runtime rewriting.
`harness/governor/reorg.py` already does bounded runtime topology adaptation and
that is the right amount. What it lacks is the guard machinery as an auditable,
first-class policy: `harness/governor/governor.py` covers wall clock (`run_s`,
`cycle_s`) and cost (`cost_cap_usd`), but there is no tool-call/token budget, no
verifier-tied stopping rule, no edit cap on reorg decisions, and repeated-state
detection lives only as a deepagents middleware
(`harness/backends/loop_guard.py`) — i.e. inside one backend, not at graph level.

### F4 — The tune loop needs held-out, OOD and replay views

**Confidence: high** (votes 3-0 ×4; four claims merged)

SEAGym showed empirically that frequent self-updates can fail to improve held-out
performance and that intermediate snapshots can collapse later (non-monotonic
replay). Among the mechanisms compared, harness-level self-editing (AHE) was the
only one that improved validation, ID and OOD together — but its broad editable
scope means one harmful harness change can affect many unrelated tasks.
Separately, objectives should be coupled to the coding domain (unit-test-filtered
trajectories, test pass rates, executable feedback). Benchmarks that preserve
persistent harness state across episodes — which self-evolving agents depend on —
remain scarce; most reset per episode.

- Sources: <https://arxiv.org/pdf/2606.17546> (SEAGym, Jun 2026),
  <https://arxiv.org/abs/2508.07407> (self-evolving agents survey, §6.2)
- Caveats: SEAGym is a single non-peer-reviewed preprint on one backend
  (DeepSeek-V4-Flash) comparing three methods, so "the only mechanism" is scoped
  to that comparison. The benchmark-scarcity claim is eroding fast (4-5 new
  preprints Apr–Jul 2026).

**Implication for harness-core.** `EvalCase` (`harness/evals/bundle.py:39`) has
`id/kind/prompt/expect/axes/weight/trials/verify_cmd` and **no split field**, so
`harness/improve/tune.py` scores and gates on the whole bundle — the monotonic
chain can be climbing a training curve. Three concrete deltas: a `split` field
(train/held/ood) enforced by the gate; a replay pass that re-scores every version
of the chain against today's bundle to catch collapse; and `verify_cmd` promoted
to a scoring axis in `harness/evals/score.py` so the objective is test pass rate,
not text axes alone. The `whatif`/`replay` CLI verbs already exist for config
mutations and are the right shape to copy.

### F5 — Investing in the harness layer is a proven lever (validates the project)

**Confidence: high** (vote 3-0)

SWE-agent demonstrated that redesigning the agent-computer interface *alone*
substantially improves SWE-bench scores with a fixed base model: 18.00% (54/300)
on SWE-bench Lite with the custom ACI vs. a shell-only baseline on the same GPT-4
Turbo, ~10.7pp from interface design alone. Replicated and accepted (NeurIPS
2024), still cited as the reference result in 2026 surveys.

- Sources: <https://arxiv.org/pdf/2606.20683>,
  <https://arxiv.org/abs/2405.15793>
- Important scoping: the companion claim that agentic benchmarks are far from
  saturated (so the headroom is mostly in the harness) was **refuted 0-3**. This
  finding says harness design *matters*, not that it is where most remaining
  headroom lives.

**Implication for harness-core.** Keep investing in the interface/harness layer —
tool ergonomics, workspace shape, verify loop — as a first-class product surface,
not plumbing. But do not market it as "the model is the commodity, the harness is
the moat"; that stronger version failed verification.

### F6 — Durable, pluggable resumability is table-stakes, not a differentiator

**Confidence: high** (votes 3-0, 3-0; two claims merged)

LangGraph's model — checkpointers persisting thread graph state, resumption keyed
by `thread_id`, swappable backends (InMemory/SQLite/Postgres) — is the 2026
baseline for the framework category, with the acknowledged cost of extra
state-modeling design work. One qualifying source (Diagrid) disputes robustness
under distributed concurrent resumption but confirms the capability framing.

- Sources: <https://docs.langchain.com/oss/python/langgraph/durable-execution>,
  <https://qubittool.com/blog/ai-agent-framework-comparison-2026>

**Implication for harness-core.** Measure the existing checkpointer against that
bar, not against ad-hoc saving. `harness/graph/checkpoint.py` is 51 lines and
hardcodes `SqliteSaver` at a fixed path (`checkpoints.sqlite` under `data_dir`);
the strict-msgpack allowlist there is genuinely good and should survive any
refactor. Missing: backend selection via config, and a resume-after-crash test
across more than one backend. Small, boring, expected — ship it, do not brag.

### F7 — Production observability means linked per-agent + orchestrator traces

**Confidence: medium** (vote 3-0, but the primary source is marketing-adjacent)

Production-grade multi-agent observability requires per-agent traces linked to
orchestrator-level traces, correlated by task; ad-hoc logging fails at the
per-agent level. The verifier rescued the claim via independent corroboration:
OpenTelemetry GenAI semantic conventions standardize hierarchical `invoke_agent`
spans with nested tool/agent children, and Honeycomb/groundcover/New Relic
2025-2026 guides converge on orchestrator + per-agent + handoff visibility.
"Requires" here is normative best practice, not an impossibility result.

- Sources:
  <https://presenc.ai/research/multi-agent-orchestration-frameworks-2026>,
  <https://github.com/open-telemetry/semantic-conventions> (GenAI conventions)

**Implication for harness-core.** The ledger (`harness/ledger/store.py`) is flat:
`runs` (one row per run), `node_events` (`run_id, node, attempt`) and
`mutations`. There is no span hierarchy, no parent/child, no per-role
attribution — and telemetry is deliberately off everywhere
(`harness/doctor.py` *fails* if `LANGSMITH_*`/`LANGCHAIN_TRACING_*` are on;
`harness/graph/checkpoint.py:26` forces it off). The privacy stance is right and
should stay; the fix is a local span tree in the ledger, with an OTLP exporter as
an opt-in flag rather than a default.

---

## Refuted claims — what NOT to build

Killed by 3-voter adversarial verification. Each one is a saved sprint.

| # | Claim | Vote | Source |
|---|---|---|---|
| R1 | Most deployed agent systems are static, so a working self-evolving tune loop addresses a gap the field considers real and largely unsolved | 1-2 | <https://arxiv.org/abs/2508.07407> |
| R2 | Agentic benchmarks remain far from saturated, so harness/runtime design (not model capability) is where the headroom is | 0-3 | <https://arxiv.org/pdf/2606.20683> |
| R3 | The field is converging on exactly harness-core's architecture (multi-model routing + harness as a learnable first-class artifact) | 1-2 | <https://arxiv.org/pdf/2606.20683> |
| R4 | A meta-learning layer over the optimizer (GRAO-style optimization-experience memory retrieving similar past failures) is *required* for stable self-improvement | 0-3 | <https://arxiv.org/html/2604.20714> |
| R5 | Co-evolving agent semantics + graph topology from a single starting agent yields 5-10% task-accuracy gains over static workflows | 1-2 | <https://arxiv.org/pdf/2509.00189> |
| R6 | A market-grade framework *must* enforce object-level authorization on every tool call, prefer narrow tools over generic shell, and guarantee durable write idempotency | 1-2 | <https://qubittool.com/blog/ai-agent-framework-comparison-2026> |
| R7 | Multiple agents are only justified when they carry separate credentials or data scopes; otherwise prefer a deterministic workflow or a single agent | 0-3 | <https://qubittool.com/blog/ai-agent-framework-comparison-2026> |
| R8 | Framework choice matters less than model selection, eval infrastructure and human-checkpoint design (so eval tooling + HITL are the higher-impact gaps) | 0-3 | <https://presenc.ai/research/multi-agent-orchestration-frameworks-2026> |

Reading the table: **R1 and R3 together mean self-evolution is not a proven
market differentiator** — treat it as unproven; build it because F2/F4 say it can
work, not because it will sell. **R4 means do not build an optimizer-memory
layer** on top of the tune loop; the stability evidence did not survive. **R5
means do not rebuild the governor as a learned co-evolution system** on the
strength of that paper — F2's per-half ablations survived, the headline gain
claim did not. **R2 means do not pitch "models are commoditized, the harness is
the moat".**

---

## Caveats on the evidence base

- Most academic sources are 2026 arXiv preprints (2603.22386, 2606.17546,
  2606.20683, 2604.20714) and are **not peer-reviewed**. HiVA (2509.00189) is
  the exception (AAAI 2026).
- SEAGym rests on one model backend and a three-method comparison; "AHE is the
  only mechanism" generalizes weakly.
- The observability finding's primary source is vendor marketing, rescued by
  OpenTelemetry corroboration.
- Claude Code's sandbox has documented escape hatches; "every command" describes
  the sandboxed path.
- **No surviving claim answers "what do paying users actually value"** — all four
  market-differentiation claims were refuted. That part of the question is open
  and needs user research / pricing-page evidence, not academic surveys.

---

## Backlog prioritized for a solo dev

Ordered by impact ÷ effort. Tags: **[TS]** table-stakes (expected; absence is
disqualifying) · **[DIF]** differentiator (few competitors have it) · **[UNP]**
unproven (evidence does not yet justify the build).

### 1. OS-level sandbox under the executor shell — [TS]

Scope: wrap the subprocess launch in `harness/backends/safe_shell.py` with
`sandbox-exec` (Seatbelt profile) on macOS and `bubblewrap` on Linux, confining
writes to the run's worktree + a session temp dir and denying outbound network by
default; the existing denylist stays as defense-in-depth, not as the boundary.
Done when: a test with the denylist explicitly disabled shows `cat /etc/passwd`
and an outbound socket both failing at the OS level, and `harness doctor` reports
which sandbox backend is active.

### 2. Graph-level guards: tool/token budget, verifier-tied stopping, loop cap — [TS]

Scope: extend `harness/governor/governor.py` beyond wall clock and cost with a
tool-call/token budget and an edit cap on reorg decisions; lift repeated-state
detection out of `harness/backends/loop_guard.py` into the graph as a
`(node, state-hash)` counter; make every cut carry an explicit `stop_reason`.
Done when: a run repeating the same `(node, state-hash)` N times is cut with
`stop_reason=loop`, a run exceeding the tool budget is cut with
`stop_reason=budget`, and both are recorded in the ledger.

### 3. Held-out split in the eval bundle, enforced by the tune gate — [DIF]

Scope: add `split: train|held|ood` to `EvalCase` (`harness/evals/bundle.py:39`)
and per-split counts to the freeze manifest; change the monotonic gate in
`harness/improve/tune.py` to require non-regression on `held`, not on the pooled
score.
Done when: a tune chain that improves `train` while regressing `held` is *not*
promoted, and the recorded reason names the split.

### 4. `verify_cmd` as a first-class scoring axis (test pass rate) — [TS]

Scope: `EvalCase.verify_cmd` already exists but does not drive the score; wire it
into `harness/evals/score.py` as a weighted axis so the coding objective is
executable feedback, not text axes alone.
Done when: a case whose `verify_cmd` exits non-zero scores 0 on the `tests` axis
regardless of the other axes, and the axis appears in `EVALUATION.md`.

### 5. Auto-allow mode gated on the sandbox — [TS] (depends on #1)

Scope: once the boundary is enforced, `harness do` / `harness run` stop prompting
for commands that stay inside the sandbox; anything attempting to leave becomes a
logged refusal rather than a prompt.
Done when: an end-to-end run completes with zero approval prompts and every
attempted escape is a ledger row.

### 6. Rollback + audit as one command — [TS]

Scope: mutations are recorded (`mutations` table) but there is no single revert
path for an applied self-edit; add `harness rollback <mutation_id>` restoring the
artifact from lineage and writing both events.
Done when: an applied mutation is reverted by one command, the working tree
matches the pre-mutation sha256, and `harness lineage` shows apply + revert.

### 7. Replay diagnostics over the tune chain — [DIF]

Scope: re-score every version of a tune chain against today's frozen bundle to
detect the non-monotonic collapse SEAGym reports (a version that looked good and
later degrades); mirror the shape of the existing `harness replay` / `whatif`
verbs.
Done when: `harness tune replay <artifact>` prints a version × split matrix and
flags any version whose retroactive score falls below its recorded score.

### 8. Pluggable checkpointer backend — [TS]

Scope: `harness/graph/checkpoint.py:38` hardcodes `SqliteSaver`; make the backend
selectable by config (memory/sqlite/postgres) while preserving the strict-msgpack
`ALLOWED_TYPES` allowlist.
Done when: a kill-mid-run + resume-by-`thread_id` test passes on two backends,
and a checkpoint from a tampered DB still refuses to deserialize arbitrary
classes.

### 9. Graph structure as a tunable artifact — [DIF] (highest research leverage)

Scope: add a topology adapter to `tunable_for` (`harness/improve/tunable.py:177`)
covering `config/topology.toml` and its `[kinds.*]` sections, reusing
`topology_evolve`'s four operators as the mutator and `topology_grammar.check` as
`validate` (illegal candidates rejected before scoring, never after).
Done when: `harness tune config/topology.toml --kind code` produces a v1..vN
monotonic chain against a frozen bundle, with the triple baseline
(`none`/`draft`/`tuned`) recorded and illegal candidates counted but unscored.

### 10. Linked per-agent + orchestrator spans in the ledger — [TS] for market, [DIF] locally

Scope: add a `spans` table (`span_id, parent_span_id, run_id, role, tool, t0, t1,
tokens_in/out, cost_usd`) following OTel GenAI naming, emitted by the graph nodes
and by the executor's fleet; keep telemetry off by default (`harness/doctor.py`
must keep failing on `LANGSMITH_*`) with an opt-in OTLP exporter behind a flag.
Done when: one run yields a tree with an orchestrator root and per-role children,
and `harness report --trace <run_id>` renders it with no network egress.

### 11. Real effect for reorg R2/R3 — [DIF] (carried from D5)

Scope: `insert_reviewer` and `collapse_fleet` are `effect="recorded"` only
(`harness/governor/reorg.py`); give them a material effect path plus auto-revert
when the triggering signal clears.
Done when: a recorded run shows the reviewer inserted and the fleet collapsed,
both reverting on their own, with the triggering signal attached to each ledger
row.

### 12. Real `task_value_usd` signal — [DIF] (unblocks #11)

Scope: `collapse_fleet` compares spend against a task value nobody computes;
derive it from unit size/kind priors plus recorded outcomes.
Done when: two units of clearly different value produce different collapse
thresholds, and the derivation is recorded alongside the decision.

### 13. OOD split sourced from real mined cases — [UNP] (depends on #3, needs volume)

Scope: use `harness/evals/mining.py` output to build an OOD split from kinds or
projects deliberately excluded from the tuned artifact's training cases.
Done when: at least one bundle carries ≥1 sealed `ood` case from a real recorded
failure and the report shows a separate OOD column — *and* the question of
whether a solo dev's volume makes that split statistically meaningful is
answered, not assumed.

### Explicitly deferred / do not build

- **Learned topology co-evolution replacing the governor's hand-written rules** —
  [UNP]; F3 says static templates suffice for repetitive verified coding work and
  R5 was refuted. Revisit only if #9 shows structural tuning paying off.
- **GRAO-style optimizer memory over the tune loop** — [UNP]; R4 refuted 0-3.
- **Self-evolution as the marketing pitch** — [UNP]; R1 and R3 refuted. The pitch
  should be safety + verified autonomy (#1, #2, #5, #6), which is what the
  evidence supports.
- **Object-level authorization on every tool call / durable write idempotency** —
  R6 refuted 1-2; not justified at solo-dev scale. Revisit only for multi-user.

### Open questions this research could not close

1. What do paying users of coding agents actually value in 2026? Every claim on
   this failed verification.
2. Can a solo dev ship Claude-Code-grade OS sandboxing on macOS with local LM
   Studio models, and what is the minimum governance layer before auto-allow can
   be trusted?
3. Where exactly does harness-core's workload fall on the static-vs-dynamic
   topology spectrum?
4. Can sealed-case mining supply enough volume for a statistically meaningful
   held-out set?
