# Harness process lifts (subject-first) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the **harness itself** (graph + prompts + tools + skills + middleware + agent roles) produce better *behavior before any judgment* — so a local Qwopus agent researches, plans, codes, and recovers like a competent process, not a lucky green verify.

**Judge role (secondary):** `/judge` (Sonnet) is only a **scoreboard after** harness changes. We do **not** optimize the judge, the rubric text, or teach axis names to the subject. If the harness improves and accept stays flat but trajectories look smarter, that still counts — measure both.

**Architecture:** Subject = harness process. Measure = outcome (verify) + wall + optional process pregrade + occasional Sonnet overall. Improve = scaffolding that makes SLMs finish work without spinning.

**Tech Stack:** harness-core (deepagents / LangGraph), LM Studio Qwopus, Claude CLI Sonnet for optional scoring, beads `harness-core-qcx` / `harness-core-ym0.5`.

## Global Constraints

- **Primary work is harness process**, not judge calibration.
- Judge stays outside the subject; never Qwopus-as-judge of itself.
- Do not teach the executor the seven axis names or "overall = weighted average".
- Mutable zones: `prompts/**`, `skills/**`, `config/agents.toml`, `plugins/**`, `benchmarks/quarantine/**`; `harness/backends/*` / `harness/skills/*` for process reliability.
- Never touch genome-immutable: ruler/genome/routing/graph nodes/uv.lock/sealed/selfapprove/skills_market.
- LMS parallel=1; idle model during measurement.
- Any KEEP must name the **harness lever** changed (prompt, skill, middleware, tool schema, agent role) — not "judge agreed".

---

## Research: improve the harness *before* judgment

Gathered 2026-08-07 (merged 2026-08-07 researcher brief). Focus: what changes **runtime behavior**, not what changes a grader.

### A) Harness is the product (Hugo Bowne — Substack)

- [How to Build an Effective Agent Harness](https://hugobowne.substack.com/p/how-to-build-an-effective-agent-harness): split **context complexity** (what to load / compact / hand off) vs **action complexity** (which tools, how to sequence, how to verify).
- Fix the failure mode you actually have: wrong tool / empty turn → action scaffolding; context blowup → progressive load + compaction; weak plan → plan mode or skip plan for tiny tasks.
- **Retest scaffolding when the model changes** — a scaffold that helps Qwopus may hurt a stronger model, and the reverse.

**Adopt:** Classify every OPEN failure as `context` | `action` | `recovery` | `verify` before picking a lift. Retest after any LMS/model swap.

### B) Procedural Intelligence (Sarah Payne — Substack)

- [Procedural Intelligence](https://sarahpayneai.substack.com/p/procedural-intelligence-how-agents): Prediction → Reasoning → Action → Verification → Recovery as **architecture**, not vibes in a prompt.
- Silent failures live at the **model↔logic seam** (missing confidence, omitted tool I/O, logic that doesn't halt).
- Modes with entry/exit guards beat "retry forever" and beat "escalate first".

**Adopt:** Prefer **deterministic gates** already in the harness (`declare_blocker`, loop_guard, EmptyTurn once, verify = exit code, read-before-write) over longer prose. New lifts should be modes/guards, not another paragraph in `executor.md`.

### C) Progressive disclosure (arXiv + agentic coding practice)

- [Is Progressive Disclosure All You Need…](https://arxiv.org/html/2607.17598): one-level skill disclosure helps when the corpus is large; a **second routing level often hurts**. Progressive disclosure buys **context**, not intelligence — redundant if the harness already navigates well.
- Pattern book / WOWHOW: Tier-1 index (short descriptions always on) → Tier-2 skill body on demand → Tier-3 references only when needed. Monolithic always-on manuals starve SLMs.

**Adopt:** Keep skill *index* tiny; load ≤2 bodies; never dump full methodology into the always-on system prompt. Audit `prompts/executor.md` + tools prompt size vs skill bodies. Prefer **shrinking always-on** over adding always-on rules.

### D) Resilience middleware peers (langgraph-kit / DeepAgents ecosystem)

Common stack that matches our pain:

| Middleware idea | Our status | Lift if missing |
|---|---|---|
| Empty-turn nudge | **Have** `EmptyTurnMiddleware` | Tune nudge text; measure retry rate |
| Loop / stall guard | **Have** loop_guard | Tighten "spinning without progress" |
| Tool-error → structured message | Partial (tool returns text) | Ensure every refusal teaches next action |
| Completion guard ("are you done?") | Weak / none | Challenge premature stop when files untouched |
| Context pressure / microcompact | ContextEditing (no SummarizationMiddleware by design) | Keep deterministic clear; never LLM-summary of run |
| Fault retries on nodes | LangGraph RetryPolicy available | Retry *transient* tool/API only — not infinite model loops |

**Adopt:** Next lifts are **CompletionGuard-class** (premature "done" without touching listed paths) and sharper tool-error shaping — not more judge axes.

### E) Local / SLM tool-loop failures (OpenCode / Ollama / LocalLLaMA)

Recurring failure modes when the model is small:

1. **Empty assistant turn** (silence) → stall — we fixed once with EmptyTurn.
2. **Thinking steals budget** / competes with `tool_calls` — LMS `reasoning_content` even when `enable_thinking: false`. Named bug cluster: LMS #1592, #827.
3. **Tool-call markup in content** (JSON / `<tool_call>` tags) instead of native tool_calls — worse on Ollama; salvage via toolcall-rescue or proxy pattern.
4. **Writes plan as chat** instead of calling `write_file` — needs first-action bias + slim tools prompt; **explicit conflict:** executor.md Step 0 vs Fluxo Steps 1–2.
5. **Context overflow** from fat tools.md / skill soup — already hit 8k context; slim `prompts/tools/openai*.md` is the right class of fix.

**Adopt:** Treat "model said the right thing in prose" as a **harness miss** if no tool ran. First-action / EXACT / ≤3-file rules stay. Measure `tool_calls_per_turn` and `empty_turn_rate` as harness KPIs. Tool-call salvage is **NEW task** — plan currently missing this entirely.

### F) Outcome fallacy (why we still measure process)

- Outcome-perfect traces can still be procedurally broken (~80%+ in cited samples). Accept alone is not "harness improved".
- Use accept + wall + **process pregrade** (code) + occasional Sonnet. Judge is **downstream evidence**, not the design surface.

---

## Research addendum: ToolMenuBench, CompletionGuard, KPI enforcement (2026-08-07 researcher)

Eight concrete lifts from research brief, ranked by leverage:

1. **Tool-call salvage** (NEW TASK) — when tool_calls empty but content has markup (`<tool_call>`, JSON), lift to structured tool_calls. Sources: toolcall-rescue, toolcall-proxy, LMS/local tool_choice issues.
2. **CompletionGuard** — deterministic check of unit output_paths before stop; one nudge max. Pattern: OpenLore `toolsWereUsed` checks.
3. **Thinking-off default + A/B** — LMS bug cluster #1592, #827. Document named evidence; measure tool_call rate vs think-ON.
4. **Causal minimal tool exposure** — ToolMenuBench arxiv 2606.15508; gate delete/task on micros. **Phase-gated tools** on unit type.
5. **Structured tool-error next-action** — smart_fs refusals; FISSION-GRPO SLM recovery gap. Every refusal teaches next concrete action.
6. **First-turn write bias** — executor.md Step 0 vs Fluxo Steps 1–2 CONFLICT — call out explicitly; prefer write over plan-then-write.
7. **Skill body word-count + Done when footer** — SELECT_LIMIT caps count not length. Enforce body limits per kind.
8. **Process KPI instrumentation mandatory** — empty_turn_rate, first_tool_turn, etc. Make Task 0 / pregrade mandatory not optional.

### Gaps explicitly added to plan:
- ✓ Tool-call salvage (Task 0b, new)
- ✓ ToolMenuBench / phase-gated tools (Task 1, expand)
- ✓ executor.md Steps 1–2 vs Step 0 contradiction (Task 1, call out)
- ✓ Skill body length enforcement (Task 3, expand)
- ✓ LMS thinking bug cluster named (Task 4, expand)
- ✓ pregrade KPIs mandatory before KEEP (Task 0, mandatory)

---

## What "harness improvement" means here (checklist)

A change counts as a harness lift only if it alters one of:

1. **Always-on protocol** — `prompts/executor.md`, tools prompt, agent role prompts (`config/agents.toml`).
2. **Progressive expertise** — skill frontmatter triggers, ranking (`loader.py`), body content, SELECT_LIMIT.
3. **Tool surface** — schemas, path conventions, refusal messages (read-before-write, shrink-guard).
4. **Loop mechanics** — EmptyTurn, loop_guard, turn budget, max_tokens, thinking flags.
5. **Topology / roles** — when planner runs vs skip; reviewer; subagents (mutable workflow/config only).
6. **Recovery** — `declare_blocker` types, structured tool errors, one-shot retries.

A change does **not** count if it only: tweaks the rubric, changes judge model, or teaches the subject the scoreboard.

---

## Failure → lever map (use before inventing prompts)

| Observed failure | Likely class | Preferred lever |
|---|---|---|
| STALL / empty content | action / recovery | EmptyTurn; max_tokens; thinking off; shorter tools prompt |
| Plans forever, never writes | action | EXACT / ≤3-file first-action; skip planner |
| Wrong skill / dream noise | context | description-first ranking; kinds/paths; SELECT_LIMIT |
| Edit fails on missing file | action | prefer `write_file` for create/rename; clearer tool docs |
| Accept but slow (rename 200s+) | efficiency | shorter protocol; fewer tools; first-write bias |
| Accept without running verify | verify | graph already owns verify — don't trust agent claim; teach "run tests" only where unit requires |
| Premature "done", files untouched | recovery | CompletionGuard / listed-path check before stop |
| Context overflow / truncated | context | progressive disclosure; microcompact; slim always-on |
| Tool-call in content, not native | action / recovery | salvage via toolcall-rescue; refusal → next action |

---

## Measurement (scoreboard only — not the product)

Freeze for a campaign: model id, LMS think setting, rubric sha (if judging), suite list.

Per case report:

| Metric | Why |
|---|---|
| accept / pass@1 / pass^3 | outcome + reliability |
| median wall_s | efficiency (harness KPI) |
| empty_turn_rate | SLM silence |
| first_tool_turn | action latency |
| process pregrade (code) | read-before-write, listed paths touched |
| `/judge` overall + min_axis | optional semantic scoreboard |

KEEP: paired A/B on same suite; name the harness lever; no accept-only KEEP for "intelligence".

---

## Task 0: Baseline the *process* (mandatory pregrade + KPIs)

**Files:** `eval/intelligence/BASELINE.md` (create), `eval/intelligence/pregrade.py` (expand)

- [x] **Step 1:** Run held_in + quarantine micros with idle LMS; log accept, wall, empty_turn_rate, first_tool_turn, tool_calls_per_turn.
- [x] **Step 2:** Add deterministic pregrade checklist (no LLM): listed paths exist after run; verify exit; optional "was read before write" from trace if available.
- [x] **Step 3:** Spot-check 3–5 traces with `/judge` only to calibrate scoreboard — do not start rubric surgery.
- [x] **Step 4:** Tag top failures with class `context|action|recovery|verify|salvage` in BASELINE.md.
- [x] **Step 5:** Commit baseline numbers into beads note / BASELINE.md. **Pregrade KPIs mandatory before any KEEP claim.**

---

## Task 0b: Tool-call salvage (NEW — missing entirely)

**Files:** `harness/backends/empty_turn.py`, possible new `harness/backends/tool_salvage.py`

- [x] Detect tool_calls empty but content has `<tool_call>` or JSON markup.
- [x] Route to toolcall-rescue pattern or parse + lift to structured tool_calls.
- [x] Measure salvage rate on baseline suite; log recovered tool names.
- [x] Optional: add to EmptyTurn middleware or standalone recovery.

---

## Task 1: Action scaffolding — finish writing + tool gating (highest leverage)

**Files:** `prompts/executor.md`, `prompts/tools/openai*.md`, `config/agents.toml`

- [x] **Explicit conflict resolution:** executor.md Step 0 (skip plan for micros) vs Fluxo Steps 1–2 (plan-then-write). Harmonize or document why both coexist.
- [x] **First-turn write bias:** Harden first-action rules (EXACT / ≤3-file / rename→write) without adding rubric vocabulary. Prefer write over plan-then-write on micros.
- [x] **Phase-gated tool exposure:** ToolMenuBench (arXiv 2606.15508) — gate delete/task/complex tools on multi-file units only; no delete on micros.
- [x] **Planner skip for micro units** (already partial) — verify no planner turn on quarantine micros.
- [x] **Tools prompt stays slim** — never reintroduce full `tools.md` for Qwopus.
- [x] **A/B:** accept + wall + first_tool_turn on rename + python_add + nextjs_page.

---

## Task 2: Recovery scaffolding — empty / premature / stuck + tool errors

**Files:** `harness/backends/empty_turn.py`, loop_guard, new `harness/backends/completion_guard.py`

- [x] Measure EmptyTurn hit rate on baseline suite.
- [x] **Design CompletionGuard:** if unit lists output paths and none written → nudge once ("files still missing"), then allow stop/blocker — no infinite loop. Pattern: OpenLore / openwork `toolsWereUsed`.
- [x] **Structured tool-error next-action:** Ensure every refusal (sha mismatch, shrink-guard, FISSION-GRPO recovery) includes the next concrete action. Sharp messages, not apologies.
- [x] A/B on content units that historically STALL under load.

---

## Task 3: Context scaffolding — progressive skills, enforce body limits

**Files:** `harness/skills/loader.py`, `skills/*.md`, trust_boundary path

- [x] Audit always-on token budget (executor + tools + skill index).
- [x] **Skill body word-count enforcement:** SELECT_LIMIT ≤2→**3** (POST_LIMIT3 KEEP); body ≤500 words per kind. Tighten descriptions as **triggers**, not manuals.
- [x] **Methodology skills:** short body = phases + stop conditions; long references stay off-prompt (agent reads file if needed).
- [x] Add "Done when" footer to each skill to clarify exit condition.
- [x] A/B content units (marketing/inventory/ecommerce) for skill noise + accept.
- [x] **`ym0.19`:** Narrow overly broad `paths=` on proc skills (durable path-flood fix). **KEEP.**
- [ ] **`ym0.20`:** Content-task wall — quarantine median ~73s (marketing/inventory). **IN FLIGHT — P1 now.**
- [ ] **`ym0.21` E3:** Procedure body cap 150w (model-aware; after `.20`).

---

## Task 4: Thinking / budget / LMS contract + named bugs

**Files:** `harness/backends/deepagents_backend.py`, serve/Cursor docs

**Model contract (from `eval/intelligence/QWOPUS_MODEL_NOTES.md`):** subject = `qwopus3.5-4b-coder-mtp` (Qwen3.5-4B + Coder SFT + MTP n=2). **`enable_thinking=false` is load-bearing** (ON + short max_tokens → empty turns). **`max_tokens=4096` binds the turn**, not 32k context. Prefer **imperative micro-procs** over abstract methodology. Do not start Opus-scale work.

- [x] **Document named LMS bugs / model notes:** `QWOPUS_MODEL_NOTES.md` + #1592/#827 cluster; thinking OFF default explained.
- [x] **Freeze settings (campaign):** `enable_thinking=false`; `DEFAULT_OPENAI_MAX_TOKENS=4096`; LMS `-c 32768`.
- [ ] **A/B think-ON vs think-OFF:** Measure tool_call rate and empty_turn_rate (same suite, same model). **DEFERRED P3 (`ym0.10`) — do not queue.**
- [ ] **`ym0.21` E1 only (P5):** optional `/no_think` prefix with thinking already OFF — not a think-ON experiment.
- [ ] KEEP only if harness KPI improves; do not chase judge scores here.

---

## Task 5: Role topology — when not to plan

**Files:** `config/agents.toml`, workflow/config if mutable

- [ ] Micro vs project: planner off for ≤3-file; on for multi-file research.
- [ ] Reviewer: only when it reduces redo, not always-on cost for micros.
- [ ] Measure planner_turns on quarantine vs held_in.

---

## Task 6: Optional scoreboard polish (do last, minimal)

**Files:** `eval/intelligence/*`, `.claude/commands/judge.md` only if broken

- [x] pregrade.py for code-visible process checks.
- [ ] `/judge` only to confirm trajectory narrative after KEEP candidates.
- [ ] **Do not** expand rubric axes until harness lifts plateau.

---

## Task 7: Campaign log

**Files:** `eval/intelligence/CAMPAIGN.md`

- [x] Each experiment: lever, class, suite, metrics, KEEP/DISCARD/INCONCLUSIVE.
- [x] Discarded scaffolds stay listed (Hugo: retest when model changes).

---

## Anti-patterns (explicit)

- Optimizing the **judge** while the subject still stalls or plans forever.
- Stuffing always-on prompts with methodology (anti progressive disclosure).
- LLM summarization of the run's own history (repo already vetoes SummarizationMiddleware).
- Teaching the subject the scoreboard axes.
- Infinite retries instead of `declare_blocker` / one-shot nudge.
- Claiming KEEP from a single noisy run.
- Skipping pregrade KPIs before KEEP; jumping to judge scores.

---

## Suggested order of work

1. Task 0 baseline + KPI instrumentation + failure classification  
2. Task 0b tool-call salvage  
3. Task 1 action scaffolding + first-write + tool gating  
4. Task 2 recovery (empty / premature + CompletionGuard)  
5. Task 3 context (skills, body limits)  
6. Task 4 thinking/budget + named bugs  
7. Task 5 roles  
8. Task 6–7 scoreboard + log  

Judge work stays at the **end of the chain**, not the front.

---

## Prime Agent reference → scarce-token SLM framing

**Brief (read this, do not clone Opus/ARC):** [`eval/intelligence/PRIME_AGENT_NOTES.md`](../../../eval/intelligence/PRIME_AGENT_NOTES.md)

The Prime+SOL vs ARC-harness chart proves **harness structure matters more than token spend**: a weak harness stays flat even at huge budgets; a structured one climbs early. For us, emulate that **steep-early SOL curve** via micro-procedures + gates on Qwopus (8k–32k context) — **not** Opus spend, ARC-AGI targets, or 150k–3M output tokens.

**Transfer only:** PTC analog (ToolSalvage + slim tools), autonomous-gate analog (CompletionGuard / Done when), path-triggered skill inject, micro-procedure chaining. **Reject:** IPython REPL tool, A2A daemon, mid-run harness CRUD, LLM summarization of run history, output-token scaling.

**Experiment fold (2026-08-07 PM):** A (tools ≤400 tok + delete gate) and B (Done when) are **already covered** by shipped lifts — do not re-file. C (denser `paths=` + optional SELECT_LIMIT=1 on ≤1-file code) → **`ym0.15`**, after `ym0.14`. Task 4 think A/B (`ym0.10`) stays **P3**.

---

## POST_PROC fold (2026-08-07 PM — `zz2`)

**Suite:** 12 micro (Qwopus). Control = post-lift; treatment = full proc library live.

| KPI | pre | POST_LIFT | POST_PROC |
|---|---|---|---|
| warn_nudge hit | 92% | 92% | **100%** ↑ |
| total nudges | 17 | 29 | **35** ↑ |
| quarantine median wall | 49s | 39s | **51s** ↑ |
| accept | 12/12 | 12/12 | 12/12 |
| ftt | — | 1.0 | 1.0 |

**Verdict:** **DISCARD** “always-on procedures.md + SELECT proc bodies” as an **empty-turn fix** (warn ↑, wall ↑). Accept held — no quality regression, but the empty-turn hypothesis failed. Middleware ceiling reached; do **not** add more middleware or more proc cards.

**Next levers then (closed):** `ym0.16` audit → `ym0.17` nudge KEEP → `ym0.18`/`mbi` SELECT_LIMIT=3+PATH_CAP=1 KEEP.

---

## POST_LIMIT3 fold (2026-08-07 PM — `harness-core-mbi` KEEP)

**KEEP stack:** middleware + procs + ym0.17 nudge + SELECT_LIMIT=3 + PATH_CAP=1.

| KPI | POST_NUDGE_ONLY | POST_LIMIT3 |
|---|---|---|
| accept | 12/12 no retry | **12/12 no retry** |
| q nudges | 30 | **19 (−37%)** |
| h nudges | 17 | **13 (−24%)** |
| h median | 70s | **69s** (PSF 109s reversed) |
| worst | 305s | **131s (−57%)** |
| q median | 57s | **73s** (accepted trade-off) |

**Next (Qwopus micro only — model-aware):**

0. **`ym0.19`** — Narrow `paths=` — **KEEP / CLOSED**
1. **`ym0.20`** — Content-task wall (q median ~73s) — **IN FLIGHT; finish first**
2. **`ym0.21`** after `.20` — order from `QWOPUS_MODEL_NOTES.md` §7:
   1. **E3** body cap 150w (P1 — same class as content wall)
   2. **E5** EmptyTurn nudge injects recovery proc (P2)
   3. **E4** rename `grep` gate (P3)
   4. **E2** max_tokens=2048 on simple micros only (P4)
   5. **E1** `/no_think` prefix (P5 — not full think-ON)

**Stay off:** Task 4 think A/B (`ym0.10`); more middleware; Opus/ARC/3M clones; authoring more `proc-*` until `.20`+E3 settle.

**Meta:** `ym0.5` owns KEEP/DISCARD measurement against POST_LIMIT3. `qcx` brainstorm is absorbed — no new design track until `.20`/`.21` land.
