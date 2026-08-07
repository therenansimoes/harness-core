# Prime Agent — Research Brief for harness-core / Qwopus SLM work

*Researcher: Cursor subagent — 2026-08-07*
*Sources: primeintellect.ai/blog/prime-agent, github.com/PrimeIntellect-ai/prime-agent README*
*Scope: design reference only — do NOT clone ARC-AGI scores or Opus-scale budgets*

---

## ⚠️ Hard scope constraint

**Our model is local and small (Qwopus / LM Studio), NOT Opus.**
Opus-scale test-time compute (600k–3M output tokens, ARC-AGI-3 curves) is
**not a goal for harness-core**. Discard any suggestion to scale output tokens
as a strategy. The relevant budget is 8k–32k context, first tool turn within
2 turns, micro-task wall time under 60s.

---

## TL;DR

Prime Agent's relevant contribution for harness-core is **not** ARC-AGI scores —
those run on Opus with hundreds of thousands of output tokens we will never have.
The transferable mechanisms are the ones that help **scarce-token SLMs**: load
only what is needed (context-as-variable / path-triggered skill inject), call
tools as direct function calls not JSON menu scrolls (PTC analog), and chain
short deterministic how-tos that each exit on a verifiable check (micro-procedure
library). Prime's steep early compute curve (Prime+SOL hits ~78% at 150k tokens)
is a shape reference — not a target — showing that structured loops convert
scarce tokens into progress, while unstructured prompting wastes them at any
token budget.

---

## 1. What Prime Agent actually does (mechanisms, not marketing)

### 1a. RLM — context as a variable

The entire session history is append-only JSONL on disk. The model runs inside
a **persistent IPython kernel** that holds its history, sub-agents, and tools
as Python variables. A compaction (`compact.run()`) cleans the live context but
the full history remains accessible from the kernel — no information is
destroyed, only moved from hot to addressable.

*Source: blog §"Session and Context Management"; README "RLM programming model"*

The practical implication: the model never suffers "I forgot what I read" —
past context lives in a variable, not in the token window.

### 1b. Programmatic Tool Calling (PTC)

The IPython kernel is the **only built-in tool**. File ops, shell commands, web
tools, sub-agents, and context management all happen as `import`-ed Python
calls. There is no JSON-schema tool dispatch loop — a tool call is
`write_file(path, content)` in a REPL, not a structured API round-trip.

```python
# Real Prime usage (from blog)
auth = await rlm("Summarize auth/ when done, reply to me", name="auth-expert")
api  = await rlm("Summarize src/ when done, reply to me", name="http-expert")
# results arrive as agent_message replies, not return values
```

*Source: blog §"RLM and Programmatic Tool-Calling"; README "Everything is programmatic"*

### 1c. Multi-agent messaging

`rlm(...)` spawns a full child agent (own model, kernel, session, history).
It returns **at admission** — like an async task handle — never at completion.
Results arrive via `agent_message.send(...)`. A2A messaging is **nuclear family
only** (parent/child/sibling), enforced by the daemon.

Persistent sub-agents survive compaction and kernel restarts; the parent
recovers them by session ID and sends follow-up turns.

*Source: blog §"Orchestration and Multi-Agent Communication"*

### 1d. Harness state CRUD — Continual Harness

Harness state `H = (ρ, G, K, M)` — prompt notes, sub-agents, skills, memories
— lives as Python objects in the kernel, written to disk on every change.

CRUD surface: `create_memory(...)`, `create_skill(...)`, `update_X(...)`,
`delete_X(...)`, `list(kind)`, `get(kind, id)`.

`/refine` reads the agent's own **trajectory**, proposes the smallest CRUD
edit that improves outcomes, plans in background (non-blocking), applies at
next turn boundary. It only edits the harness layer — the base system prompt
is immutable and rollback is supported.

*Source: blog §"Self-Improvement via the Continual Harness"; README "Continual Harness"*

### 1e. What "RLM" means

Recursive Language Model: context is a variable, sub-agent delegation is a
function call, and the model writes "language model programs" as actions over
its own context. It does NOT mean a training loop or RL — it is a runtime
architecture where the model treats its own session as a manipulable data
structure.

*Source: blog opening; README first paragraph*

---

## 2. What the chart teaches (and what it doesn't)

**Verified from blog §"Evaluating Prime Agent":**

| Config | ARC-AGI-3 score | Token regime |
|---|---|---|
| Opus 5 + Prime Agent | 95.5% | ~600k output tokens per game |
| Opus 5 + Claude Code (ARC harness) | ~30.2% | Flat regardless of spend |
| GPT-5.6 SOL + Prime Agent | ~78% @ ~150k tokens | Steep early curve |
| GPT-5.6 SOL + ARC harness | ~13% even at 3M tokens | Stays weak |

**What the chart says that applies to us:**

The harness structure matters more than the token budget. A *poorly structured*
harness stays weak even at 3M tokens (SOL + ARC harness). A *well-structured*
harness extracts early value at ~150k tokens (Prime+SOL) before the curve
flattens. For Qwopus, we live in the leftmost part of that curve — at 8k–32k
context per turn. The lesson is **loop structure at small token budgets**, not
"spend more tokens."

**What the chart does NOT say for us:**

- Do not target ARC-AGI-3 scores.
- Do not target 150k or 600k output tokens — those are 5–50× our context window.
- Do not optimize for Opus or SOL model capability. Our model is Qwopus local.
- "Scaling output tokens" is not a strategy when the model has a 32k context cap
  and 4–8GB RAM.

**The Qwopus micro-procedure thesis:**

Many short how-tos chained = how a small model climbs the left side of the
curve. Each procedure is a single verifiable step (read → write → verify → exit).
Tokens spent on structure compound across steps; tokens spent on planning prose
are burned without changing disk state.

---

## 3. What transfers to harness-core

| Prime mechanism | harness-core surface | Transfer shape |
|---|---|---|
| PTC — tool as function call | `harness/backends/tool_salvage.py` | **Already shipping.** Rescues Hermes/bare-JSON markup that lands in content instead of `tool_calls`. Extend: Qwopus emit-format variants. |
| Empty turn recovery | `harness/backends/empty_turn.py` | **Already shipping.** One nudge on silence. Transfer: measure rate; tune nudge text; instrument `first_tool_turn`. |
| Completion gate (autonomous-gate) | `harness/backends/completion_guard.py` | **Already shipping.** Nudge once when expected output files missing at stop. This IS the Prime autonomous-gate analog. |
| Harness state CRUD — skills | `skills/*.md` + `harness/skills/loader.py` SELECT_LIMIT | **Partial.** Static skill files, not online CRUD. Transfer: enforce short bodies (≤500 words), "Done when" footer as exit guard, `paths` glob trigger for deterministic selection. |
| Progressive skill disclosure | `select_skills()` ranking in `loader.py` | **Already.** SELECT_LIMIT=2, token-ranking, path triggers. Transfer: keep index tiny, never dump full methodology. |
| Compaction (non-rewrite) | `ContextEditingMiddleware` clears old tool output | **Compatible.** Prime's compaction is also NOT LLM-rewrite of run history — deterministic clearing. SummarizationMiddleware veto stays. |
| `declare_blocker` / stall guard | `loop_guard`, `declare_blocker` typed | **Already.** Strengthening blocker types > adding retries. |
| Phase-gated tools | `executor.md` Step 0 + tool gating (Task 1 plan) | **Planned.** Gate `delete`/`task`/complex tools on multi-file units only. |
| Goal + heartbeat (autonomous) | max_turns + governor | **Structural analog.** Turn budget + governor = bounded continuation. No heartbeat needed for micro suite. |
| Micro-procedure chaining | skills as micro how-tos | **Design gap.** See §5 below. |

---

## 4. What does NOT transfer

| Prime mechanism | Why it doesn't apply |
|---|---|
| Persistent IPython REPL as the model tool | harness-core uses LangGraph graph nodes with a defined tool schema. Replacing this would touch `harness/graph/**` (genome-immutable topology). |
| Agent-to-agent messaging daemon | Our topology owns routing; adding a message bus requires a new graph node (genome-immutable) or a plugin node. Not worth it for Qwopus micro suite. |
| `/refine` online harness CRUD | Writing to prompts/skills mid-run is mutable-zone behavior only via the improve loop + genome check. Mid-run CRUD is not safe and would poison the sealed exam KPI baseline. |
| SummarizationMiddleware-style compaction | **Explicitly vetoed** (AGENTS.md): "a model that rewrites the run's own history makes the ledger measure a summary." |
| ARC-AGI-3 eval as a target | Wrong benchmark for SLM/local work. Use `accept + wall_s + empty_turn_rate + first_tool_turn` on held_in/quarantine micro suite. |
| Reward hacking / self-modification of ruler | Genome-immutable. `harness/ruler/**` and `harness/genome/**` are never touched by agents. Prime's Factorio cheating episode is exactly why. |
| Model training on harness | Not applicable — we consume models, don't train. Prime's "model-harness co-learning" future is irrelevant for this campaign. |

---

## 5. Micro-procedure library implications

Prime's steep early curve comes from structural loops, not prose instructions.
For harness-core Qwopus work, a **micro-procedure library** is the equivalent:
each skill is a short, deterministic how-to that exits when a verifiable check
passes.

**Design constraints (from harness-core code + Prime lessons):**

- Skill body ≤ 500 words (SELECT_LIMIT=2 already caps count, not length — add
  length enforcement per skill kind).
- Each skill ends with a **"Done when"** footer: a deterministic exit condition
  (`verify exit 0`, `file exists at path`, `no error in output`), not "seems
  right."
- Skills are **single-phase**: one concern, one tool call class, one verify.
  Chain is composed by the graph/executor, not by a mega-skill.
- `paths` glob frontmatter for deterministic selection on known file types —
  already supported in `loader.py`.

**Procedure cards to add or improve (file as beads):**

| Card | Mechanism | harness surface |
|---|---|---|
| `rename-symbol` | write_file whole small file; exit when verify passes | `skills/python-methodology.md` + executor.md Step 0 |
| `create-file-from-spec` | write_file at spec path; verify exists | executor.md EXACT rule hardening |
| `edit-single-function` | read → edit → verify; no planner | executor.md Step 0 + `skills/python-fixes.md` "Done when" footer |
| `recover-empty-turn` | tool salvage → nudge → retry once | `EmptyTurnMiddleware` + `ToolSalvageMiddleware` (both shipping) |
| `verify-then-exit` | run verify_cmd; pass = exit; fail = one structured recovery | `CompletionGuardMiddleware` (shipping) |
| `phase-gate-tool-exposure` | expose only write_file+edit_file on ≤3-file micros | executor.md + `prompts/tools/openai*.md` slimming |
| `structured-error-next-action` | every refusal includes next concrete step | smart_fs, shrink-guard messages |
| `path-trigger-skill` | `paths = ["*.py"]` frontmatter → deterministic inject | `loader.py` already supports; add to python skills |

---

## 6. Recommended experiments (judge-blind, tokens→progress efficiency)

**Scope:** All experiments target scarce-token Qwopus/LMS behavior only.
Paired A/B on held_in micro suite. KPIs: `empty_turn_rate`, `first_tool_turn`,
`wall_s`, `accept`. **No ARC-AGI. No Opus. No output-token scaling. No
judge-first KEEP.**

### Experiment A: Tool-call structure budget (Prime PTC analog)

**Prime mechanism being transferred:** PTC removes the "tool menu to scroll"
problem. Qwopus analog: fewer schema tokens in the always-on context window
means the model reaches the first tool call sooner.

**Lever:** slim `prompts/tools/openai*.md` to ≤ 400 tokens; gate `delete` and
`task` tools off for ≤3-file units (`config/agents.toml`).

**Hypothesis:** fewer schema tokens → lower `first_tool_turn`; no delete
distraction → lower `empty_turn_rate` on simple micros.

**Measurement:** `first_tool_turn` + `wall_s` on `micro_refactor_rename`,
`micro_python_add`, `micro_two_files`.

**Not the goal:** do not measure ARC scores; do not measure at 150k+ output
tokens; do not A/B at frontier model scale.

### Experiment B: Micro-procedure "Done when" exit guards

**Prime mechanism being transferred:** `/autonomous --gate` prevents the agent
finishing before verifying. Qwopus analog: a deterministic exit condition in
each skill body prevents spinning and premature "done" without a tool call.

**Lever:** add `## Done when` footer to `skills/python-methodology.md`,
`skills/python-fixes.md`, and one content skill. Each footer is one line:
`verify exit 0 and path exists` — not prose.

**Hypothesis:** explicit exit condition lowers `empty_turn_rate` on content
units that historically stall within a 32k-token budget.

**Measurement:** `empty_turn_rate` + `accept` on `micro_content_summary`,
`micro_marketing_cta`.

### Experiment C: Path-trigger deterministic skill inject

**Prime mechanism being transferred:** Prime kernel pre-imports skills as Python
modules on init — deterministic, zero-search. Qwopus analog: `paths` glob
frontmatter in `loader.py` fires the right skill on the first turn without
fuzzy ranking burning tokens.

**Lever:** add `paths = ["*.py"]` frontmatter to `skills/python-methodology.md`
and `skills/python-fixes.md`; enforce `SELECT_LIMIT = 1` for `code` kind on
≤1-file units.

**Hypothesis:** right skill on first turn → lower `first_tool_turn` and lower
always-on context pressure.

**Measurement:** `first_tool_turn` + which skill fired (log) on
`micro_refactor_rename`, `micro_edit_line`.

---

## 7. Sources

| Source | One-liner |
|---|---|
| https://www.primeintellect.ai/blog/prime-agent | Full blog post; primary source for all mechanism descriptions |
| https://github.com/PrimeIntellect-ai/prime-agent README | Open-source README; confirms blog claims, adds CLI and architecture docs |
| `harness/backends/empty_turn.py` | EmptyTurnMiddleware — shipping nudge for silence |
| `harness/backends/tool_salvage.py` | ToolSalvageMiddleware — shipping Hermes/JSON tool rescue |
| `harness/backends/completion_guard.py` | CompletionGuardMiddleware — shipping stop-gate analog |
| `harness/skills/loader.py` | `select_skills()`, SELECT_LIMIT=2, path-trigger mechanism |
| `prompts/executor.md` | Always-on executor protocol, Step 0 (micro fast-path) |
| `docs/superpowers/plans/2026-08-07-harness-intelligence-lifts.md` | Existing campaign plan; this brief extends, does not duplicate |

---

---

## Explicit rejection of Opus-scale goals

The following are **out of scope for harness-core** and must not be filed as
beads or added to the campaign plan:

- Targeting ARC-AGI-3 scores at any level.
- "Scaling output tokens" as a harness strategy.
- Test-time compute experiments at 150k–3M output tokens.
- Optimizing for Opus 5 or GPT-5.6 SOL behavior — our model is Qwopus local.
- Any KEEP criterion that requires frontier-model budgets to reproduce.

The chart is evidence that **structure beats spend**. For Qwopus, structure
is the only lever we have.

---

*Do not implement code from this brief. File beads for each experiment above
and update the campaign plan with a "Prime Agent reference → scarce-token SLM
framing" section pointing here (`eval/intelligence/PRIME_AGENT_NOTES.md`).*
