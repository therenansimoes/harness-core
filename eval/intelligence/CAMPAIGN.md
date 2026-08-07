# CAMPAIGN — Harness process lifts (subject-first, 2026-08-07)

Product = harness behavior. `/judge` = optional scoreboard **after** KEEP candidates only.

## Design lock — SLM ≠ Claude/Cursor

**Subject model (frozen for this campaign):** `qwopus3.5-4b-coder-mtp` — Qwen3.5-4B dense + Jackrong Coder SFT + MTP speculative decode (`n=2`). Source of truth: [`QWOPUS_MODEL_NOTES.md`](QWOPUS_MODEL_NOTES.md).

**Load-bearing inference contract (do not casually A/B):**
- **`enable_thinking=false`** — thinking ON + tight `max_tokens` → empty `content` / no `tool_calls` (EmptyTurn). Stay OFF. Full think ON/OFF suite = `ym0.10` P3 only.
- **`DEFAULT_OPENAI_MAX_TOKENS=4096`** — binds the turn. **Loaded ctx = 8 192** (Q4_K_S, user LMS 2026-08-07; earlier notes said 32k — corrected). With always-on prompts (~1700 tok) + 3 skill bodies (~1950 tok) + max_tokens=4096, the KV has ~440 tok margin for accumulated tool output. KV is the binding constraint now, not attention.
- **Imperative micro-procs** — numbered steps, named tools, situation header first, bodies short (≤500w cap; E3 150w INCONCLUSIVE pending idle-LMS re-run). Abstract methodology / long CoT prompts evaporate on 4B **and** eat the scarce KV.

Qwopus has **short working memory** (KV decays after ~15 tool turns AND loaded ctx is only 8k). A good harness is **many micro procedures chained and SHORT** (situation → short how-to → next situation), not one fat `executor.md` + fat methodology skills. **Context headroom is NOT fine — 8k loaded is tight.** Per-call token budget (`max_tokens`) AND skill body size are both binding.

| Claude / Cursor / Opus | Qwopus 4B Coder MTP harness |
|---|---|
| Long-horizon plan stays in context | Plan evaporates; each step must be a **recoverable procedure** |
| Fat manuals OK | Fat manuals waste attention and get ignored |
| Thinking/CoT as free quality | Thinking steals `max_tokens` → empty turns; **keep OFF** |
| One agentic flow | **Procedure library + gates** (salvage/empty/completion already ship) |

Progressive disclosure (`SELECT_LIMIT=3` + `PATH_CAP=1`, **500w body cap**) is **necessary but not sufficient** — we still need **breadth** of imperative micro how-tos. **Anti-pattern:** stuffing `executor.md`; Opus-scale budgets/ARC/3M tokens. Design: index always-on; ≤3 short procedure bodies at a time. (E3 150w INCONCLUSIVE — idle-LMS re-run needed)

Workstream: **`ym0.13`** → **`ym0.14`** (cards) → selection KEEP (`ym0.17`–`ym0.19`) → content wall (`ym0.20`) → model-aware A/Bs (`ym0.21`).

## Status board (PM — POST_LIMIT3 KEEP)

**KEEP stack (frozen control):** middleware (SmartFS→…→EmptyTurn→CompletionGuard) + proc library/index + **ym0.17 nudge** + **SELECT_LIMIT=3** + **PATH_CAP=1**.

| Lane | Item | Reality |
|---|---|---|
| **Done** | Tasks 0–3 (pregrade, salvage, SmartFS/tools, CompletionGuard, skill caps) | Verified intact; salvage restored; mechanic **114 tests** |
| **Done** | Integrity `ym0.12` | Closed (PM + mechanic) |
| **Done** | EmptyTurn KEEP / rename ACCEPT | `ym0.6` closed |
| **Done** | Re-baseline post-lift | **`ym0.8`** CLOSED — 12/12 accept (1 retry); 92% warn_nudge (=); quarantine median −21%; json_transform −63%; ftt=1 all; avg tpt=0.82; proc partial (see BASELINE POST_LIFT) |
| **Done** | POST_PROC clean pass (procs fully live) | **`zz2`** CLOSED — 12/12 accept (1 retry); warn_nudge **100% ↑**; q median **51s ↑**; **DISCARD** “load proc bodies via SELECT as empty-turn fix” |
| **Done** | SLM micro-procedure library design | **`ym0.13`** — `eval/intelligence/MICRO_PROCEDURES.md` |
| **Done** | CLI trace copy | **`ym0.9`** — `_save_trace` for pregrade KPIs |
| **Done** | Author v1 procedure cards | **`ym0.14`** — 10 `skills/proc-*.md` + `prompts/procedures.md` index wired into backend |
| **Done** | Path-trigger denser use (Prime Exp C) | **`ym0.15`** — denser `paths=`; SELECT_LIMIT=1 skipped |
| **Done** | ym0.17 KEEP (nudge naming) | POST_NUDGE_ONLY: held_in no-retry; two_files+json_transform nudges halved. Quarantine spike from path-flood is ym0.18 regression, not ym0.17. |
| **Done** | ym0.18 redesign: SELECT_LIMIT=3 + PATH_CAP=1 — **KEEP** | **`harness-core-mbi`** CLOSED — POST_LIMIT3: 12/12 no retries; q nudges 19 (−37% vs PNO); h nudges 13 (−24%); total 32 (−32%); held_in median 69s (PSF blowup reversed); worst 131s (−57%). All 3 gates pass. |
| **Model notes** | Deep-dive on Qwopus3.5-4B-Coder-MTP | **`QWOPUS_MODEL_NOTES.md`** landed — thinking OFF + max_tokens=4096 load-bearing; 5 judge-blind experiments under **`ym0.21`** |
| **Done** | Narrow overly broad `paths=` | **`ym0.19`** KEEP — .py path-flood 6→3; PATH_CAP less load-bearing |
| **Done** | Content-task wall (SELECT body cost) | **`ym0.20`** INCONCLUSIVE — live: actual delta = 1/4 content units; 3 confounds block isolation; idle-LMS re-run needed |
| **INCONCLUSIVE** | E3 body cap 150w — live 12-micro: 12/12 accept, nudge +19%, wall confounded | **`ym0.21` E3** — POST_E3: 12/12 accept (no fails); proc-* cards were mostly <150w at E3 time (now 143–177w, majority exceed 150 — limit reverted to 500); offline projection of 3 failures was wrong; wall invalid (LMS not idle); Agent A DISCARD overturned (wall = LMS artifact); reconciled INCONCLUSIVE; clean idle-LMS re-run needed |
| **Next** | Idle-LMS clean re-run to isolate ym0.20 + E3 (both INCONCLUSIVE) | unblock before ym0.22/E2 |
| **Watch** | Empty-turn prevention (still 100% hit) | Middleware ceiling; ftt=1.0; nudge −32%. E5 is the only queued non-middleware poke |
| **Later** | Task 5 role topology | **`ym0.11`** — after empty-turn levers plateau |
| **Deferred** | Task 4 think ON/OFF A/B | **`ym0.10`** → **P3** — stay off (E1 is `/no_think` only, not full think-ON) |
| **Meta** | Score loop KEEP/DISCARD | **`ym0.5`** — still owns measure-after-KEEP; control = POST_LIMIT3 |
| **Meta** | Brainstorm parent | **`qcx`** — absorbed into campaign + model notes; no new design work until .20/.21 land |

## Middleware stack (verified)

```
SmartFS → ModelCallLimit → TodoList → ContextEditing
→ LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry
```

## SLM micro-procedures (workstream)

**Goal:** procedure cards the harness can **select and chain** without blowing context.

**Design: `eval/intelligence/MICRO_PROCEDURES.md`** (filed by `ym0.13`) — full inventory, gap list ranked by Qwopus leverage, chaining rules, card spec for `ym0.14`.

**Chaining (validated in `ym0.13`):**

1. **Always-on:** `prompts/procedures.md` index (~200 w: name + 1-line trigger only) + slim tools + executor Protocol 0 gates. ~1 300 w total always-on.
2. **Per turn / per situation:** ≤ `SELECT_LIMIT=3` **bodies** (≤ 500 w each; **PATH_CAP=1** on path-triggered) matched by keywords / kind / failure class. Priority: recovery > action > domain.
3. **Gates already own recovery:** EmptyTurn / ToolSalvage / CompletionGuard nudge → next procedure should be the *named* recovery card (`recover-after-empty-turn`, `recover-missing-files`), not more prose in executor.md.
4. **Domain methodology skills** stay triggers for content/code domains; **situation procedures** (`proc-*`) cover action/recovery/verify micro-scopes.
5. **Exit condition explicit on every card:** "Done when" clause; model stops, does not retry loop.

**Top 10 gaps ranked by Qwopus leverage (`ym0.13` confirmed):**

| rank | class | candidate micro how-to | leverage |
|---|---|---|---|
| 1 | recovery | `recover-after-empty-turn` | EmptyTurn fires 11/12; no procedure card |
| 2 | verify | `verify-then-stop` | every micro; false-greens are worst outcome |
| 3 | action | `exact-write-from-spec` | most common fast path; replaces executor reliance |
| 4 | recovery | `recover-missing-files` | CompletionGuard fires; no "do THIS next" card |
| 5 | action | `rename-via-write` | highest error-surface action for SLMs |
| 6 | action | `two-module-create` | model writes one of two; needs explicit "write BOTH" |
| 7 | action | `surgical-edit-check` | edit_file silent fail on non-unique old_string |
| 8 | recovery | `declare-blocker` | model loops when stuck; no card |
| 9 | verify | `check-listed-files` | pre-stop checklist; catches CompletionGuard early |
| 10 | action | `content-cta-skeleton` | structure card; avoids loading fat domain skill |

**Gap cards ranked (confirmed in `ym0.13`):**

## Shipped levers (Tasks 0–3)

| lever | class | status |
|---|---|---|
| pregrade + BASELINE (pre-lift) | process | SHIPPED — needs POST_LIFT |
| EmptyTurn / ToolSalvage / CompletionGuard | recovery/salvage | SHIPPED |
| `_fs_tools_for` + micros omit delete | action | SHIPPED |
| executor Fluxo → Protocolo 0 | action | SHIPPED |
| `files=` on 12 micros | recovery | SHIPPED |
| skill 500w + Done when | discipline | SHIPPED — not yet breadth |

## Baseline headline (pre-lift — stale for KEEP)

**12/12 accept** · **92% empty_turn hit rate** · SmartFS OFF at freeze · per-turn KPIs unknown until `ym0.9`.

## Post-lift headline (POST_LIFT + partial PROC — 2026-08-07)

**12/12 accept (1 retry)** · **92% warn_nudge hit rate unchanged** (11/12) · SmartFS ON · ftt=1.0 all runs · avg tpt=0.82 · quarantine median wall −21% (49→39s) · json_transform −63% (199.7→74.3s) · etr=0.0 is middleware artifact (empty turns eaten before trace write) · proc cards partial (quarantine pre-proc, held_in post-proc). **POST_PROC clean pass needed — ym0.8 note filed.**

## POST_PROC headline (ALL lifts live — 2026-08-07)

**12/12 accept (1 retry)** · **warn_nudge hit rate UP: 100%** (12/12 ↑ from 92%) · total nudges **35** (↑ from 29 post-lift / 17 pre) · ftt=1.0 all · avg tpt=0.84 · quarantine median wall **+31% (39→51s)** · held_in median −22% (68→53s, noisy) · json_transform +3% · refactor_rename +27% · etr=0.0 artifact unchanged.

### Verdict — procedure-body load as empty-turn fix

| Claim | Verdict | Evidence |
|---|---|---|
| Always-on `procedures.md` index + SELECT of up to 10 `proc-*` bodies reduces empty turns | **DISCARD** | warn_nudge **92% → 100%**; total nudges **29 → 35**; middleware already catching empties (ftt=1.0) |
| Same lever improves wall on quarantine micros | **DISCARD** | q median **39s → 51s** (+31%, past pre-lift 49s) — context cost |

**Board conclusion (do not ignore):** Proc cards + always-on index did **not** reduce empty turns; they add context cost. **Middleware ceiling reached.** Next levers are **not** more middleware and **not** stuffing more proc cards.

KEEP gate (unchanged): +2 accept and/or −20% empty_turn warn rate vs **post-lift** control, sustained ≥2 runs.

## Risks

- Parallel-agent file stomps on `harness/backends/*` (salvage briefly deleted once).
- Stale BASELINE; CLI trace gap (`ym0.9`).
- **Anti-pattern:** expanding `executor.md` instead of adding micro procedures.
- LMS contention (parallel=1, idle model).

## Next builder launches (post–POST_LIMIT3 KEEP)

| # | Lever | Why | Launch? |
|---|---|---|---|
| **A** | **`ym0.16`** selection audit | path-flood root cause | **CLOSED** |
| **B** | **`ym0.17`** EmptyTurn nudge naming | held_in no-retry; orthogonal KEEP | **KEEP / CLOSED** |
| **C** | **`ym0.18` / `mbi`** SELECT_LIMIT=3 + PATH_CAP=1 | q/h nudges ↓; PSF blowup reversed; 12/12 no retry | **KEEP / CLOSED** |
| **1** | **`ym0.19`** Narrow overly broad `paths=` on procs | Durable fix under KEEP stack | **KEEP / CLOSED** |
| **2** | **`ym0.20`** Content-task wall (q median ~73s) | marketing/inventory residual cost of limit=3 | **INCONCLUSIVE** — live confounded (3 simultaneous changes + LMS not idle); inventory_reorder signal positive (7→2 nudges) |
| **3** | **`ym0.21`** model-aware A/Bs (E1–E5) | Qwopus-tuned; see order below | **E3 INCONCLUSIVE** (no failures; nudge +19%; wall confounded); clean re-run needed before issuing verdict |
| — | More middleware / Task 4 think (`ym0.10`) | ceiling / off-path | **STAY OFF** |
| — | Opus / ARC / 3M-token clones | out of campaign forever | **NEVER** |

**Control = POST_LIMIT3.** Revert `_BODY_WORD_LIMIT` to 500 before running E2 (`ym0.22`).

### `ym0.21` experiment order (8k ctx corrected 2026-08-07)

Judge-blind process KPIs only. Source: `QWOPUS_MODEL_NOTES.md` §7. Loaded ctx = **8192** (not 32k) → E3 load-bearing; E2 promoted.

| Pri | Exp | Lever | Status / why |
|---|---|---|---|
| **INCONCLUSIVE** | **E3** | Procedure body cap **150w** (vs 500) | Live 12/12 accept; proc-* cards mostly <150w at run time (since grown to 143–177w; 8/10 now exceed 150); nudge +19%; wall confounded (LMS not idle); executor.md Protocol 0 confound; Agent A DISCARD claim overturned (wall = LMS artifact). **Limit reverted to 500.** Idle-LMS re-run needed before issuing KEEP/DISCARD. |
| **DISCARD** | **E2** | `max_tokens=2048` on ≤2-edit micros only | **`ym0.22`** DISCARD — reverted 2026-08-07. Heuristic failed: task-simplicity ≠ short output; `nextjs_page` (allowlist→SIMPLE): +800% nudges, +247% wall. Lesson: next redesign needs output-size signal not file-count allowlist. Code removed; E2 retry filed P3. |
| **P2** | **E5** | Inject `proc-recover-after-empty-turn` in EmptyTurn nudge (bypass PATH_CAP) | Only queued poke at 100% empty-hit without new middleware |
| **P3** | **E4** | `grep` confirmation gate in `proc-rename-via-write` | Imperative SLM pattern; skill-only |
| **P4** | **E1** | `/no_think` user prefix (thinking already OFF) | Marginal; do **not** expand into `ym0.10` think-ON |

**Do not launch `ym0.10` (think A/B).** Do not add middleware. E2 DISCARD — `DEFAULT_OPENAI_MAX_TOKENS` stays 4096 universally (code & tests removed 2026-08-07).

## Prime Agent → scarce-token SLM (brief landed)

**Source of truth:** [`eval/intelligence/PRIME_AGENT_NOTES.md`](PRIME_AGENT_NOTES.md) · [blog](https://www.primeintellect.ai/blog/prime-agent) · [repo](https://github.com/PrimeIntellect-ai/prime-agent).

The chart proves harness structure beats spend (Prime+SOL climbs early; ARC harness stays weak at 3M tokens). For us: emulate that **steep-early SOL curve** with micro-procedures + middleware gates on Qwopus — **not** Opus budgets, ARC-AGI, or output-token scaling.

| Researcher exp | Transfer | Status vs shipped |
|---|---|---|
| A tools ≤400 tok + delete gate | PTC / ToolMenuBench | **COVERED** — `prompts/tools/openai*.md` ~230 tok; micros omit delete / SmartFS `tools=` |
| B Done when footers | autonomous-gate analog | **COVERED** — methodology (+ proc) skills already have `## Done when` |
| C denser `paths=` (+ optional SELECT_LIMIT=1 on ≤1-file code) | path-trigger inject | **DONE `ym0.15`** — 6 skills now have `paths=` (`proc-rename-via-write`, `proc-surgical-edit-check`, `proc-two-module-create`, `proc-exact-write-from-spec`, `proc-content-cta-skeleton`, `python-fixes`); recovery cards stay paths-empty; SELECT_LIMIT=1 skipped (no clean hook without graph surgery) |

**Out of scope forever for this campaign:** IPython REPL-as-tool, A2A messaging daemon, mid-run harness CRUD, SummarizationMiddleware, Opus/ARC score chasing.

## Experiments log

| id | lever | class | suite | KPIs | verdict |
|---|---|---|---|---|---|
| pre-lift | (control) | — | 12 micro | accept 12/12; warn_nudge hits 11/12 (92%); median q:49s h:55s; worst 199.7s; SmartFS OFF | FREEZE |
| post-lift+partial-proc | SmartFS+ToolSalvage+EmptyTurn+CompletionGuard; proc cards partial (quarantine pre-proc, held_in post-proc) | recovery/salvage/action | 12 micro | accept 12/12 (1 retry); warn_nudge hits 11/12 (92%=); ftt=1.0 all; avg tpt=0.82; median q:39s (−21%); json_transform 74.3s (−63%); etr=0.0 trace artifact | CONTROL — needs clean POST_PROC pass |
| **POST_PROC** (`zz2`) | SmartFS+ToolSalvage+EmptyTurn+CompletionGuard+10 proc cards+procedures.md+paths= densify; ALL lifts live | recovery/salvage/action/procedure | 12 micro | accept 12/12 (1 retry); **warn_nudge 12/12 (100% ↑)**; nudges 35 ↑; ftt=1.0; avg tpt=0.84; median q:**51s (+31%)**; h:53s; json_transform 76.9s; refactor_rename 143s | **DISCARD** as empty-turn fix (wall regression; warn ↑). Current control for next A/Bs. Middleware ceiling. |
| **`ym0.16`** | selection audit — path-flood root cause; logging added to `select_skills` | observability | offline sim (11 micros) | PATH-FLOOD confirmed; recovery procs innocent; body budget OK | **CLOSED** — unblocks B+C |
| **`ym0.17+ym0.18`** (POST_SELECT_FIX) | ym0.17: nudge text names `proc-recover-after-empty-turn` + output paths; ym0.18: PATH_CAP=1 on triggered | recovery/ranking | 12 micro live | accept 12/12 (1 retry); **warn_nudge 10/12 (83%, −17pp)**; q nudges **8 (−50%)**; h nudges **29 (+53%)**; total 37; ftt=1.0; tpt=0.80; median q:54s (+6%); h:109s (+106%); worst 331.3s | **INCONCLUSIVE** — 83% hit (gate ≤80% not cleared); PATH_CAP=1 degrades multi-file held_in (two_files 2→8; refactor_rename 5→9+retry). ym0.17 shows quarantine signal. Next: isolate ym0.17 alone. |
| **`ym0.17` isolated** (POST_NUDGE_ONLY) | ym0.17 only: nudge text names proc-recover-after-empty-turn; PATH_CAP reverted | recovery/ranking | 12 micro live | accept 12/12 **no retries**; warn_nudge 12/12 (100%); q nudges 30 (up vs POST_PROC 16); h nudges **17 (−12 vs PSF 29)**; total 47; no held_in retry; two_files 8→2; json_transform 6→2; refactor_rename 9→6; median q:57s; h:70s; worst 305s (rrw volatile) | **KEEP ym0.17** — held_in no-retry; two_files+json_transform halved; quarantine spike from path-flood without PATH_CAP (rrw 0→12, inv 2→11); ym0.17 orthogonal and safe. Next: ym0.18 redesign SELECT_LIMIT=3 + PATH_CAP=1 (harness-core-mbi). |
|| **`ym0.18` redesign** (`harness-core-mbi`) | SELECT_LIMIT=3 + PATH_CAP=max(1,limit//3)=1; overflow triggered falls back to desc-ranking | ranking | 12 micro live (POST_LIMIT3) | accept 12/12 **no retries**; quarantine nudges **19 (−37% vs PNO 30)**; held_in nudges **13 (−24% vs PNO 17)**; total **32 (−32% vs PNO 47)**; held_in median **69s** (PSF 109s blowup reversed); worst case **131s** (PNO 305s); 3/3 KEEP gates pass | **KEEP** — all gates pass; no retries; both nudge directions improved; held_in wall stable |
| **`ym0.19`** (`harness-core-ym0.19`) | narrow `paths=` on procs: remove from `proc-exact-write-from-spec` and `proc-surgical-edit-check` (language-agnostic; description wins); deduplicate `["**/*.py","*.py"]` → `["**/*.py"]` on rename/two-module/python-fixes | ranking/selection | offline sim only (selection smoke) | `python_add` → `[python-methodology, proc-two-module-create, proc-recover-missing-files]` ✓; `edit_line` → `[python-methodology, proc-rename-via-write]` (minor: surgical-edit-check still loses to rename by desc score — acceptable); `exact_write` → `[python-methodology, proc-exact-write-from-spec, proc-recover-missing-files]` ✓; `refactor_rename` → `[python-methodology, proc-rename-via-write, method-planning]` ✓; 28/28 test_skills pass | **KEEP** — .py path-flood from 6→3 triggered skills; PATH_CAP=1 less load-bearing; remeasure live with ym0.20 |
| **`ym0.20`** (`harness-core-ym0.20`) | `CONTENT_SELECT_LIMIT=2` in loader | ranking/selection | offline sim + live 12-micro (confounded) | offline: content 3→2 skills; tests pass. Live: actual delta = 1/4 content units (inventory_reorder only; others already ≤2). inventory_reorder nudges 7→2 (positive). 3 confounds: E3 + executor.md Protocol 0 + LMS not idle. | **INCONCLUSIVE** — offline claim overstated; single-unit signal; idle-LMS isolated re-run needed |
| **`ym0.21` E3** (`harness-core-ym0.21`) | `_BODY_WORD_LIMIT` 500→150; render-time only — skill .md files unchanged | body-cap / context cost | 12 micro live 2026-08-07 (confounded: LMS not idle + executor.md Protocol 0) | 12/12 accept (no verify failures — offline projection of 3 failures was wrong); q nudges 21 (+11% vs PL3 19); h nudges 17 (+31% vs PL3 13); total 38 (+19%); proc-* cards mostly <150w at run time (since grown to 143–177w; 8/10 now exceed 150; limit reverted to 500); wall invalid (4 extra LMS models). Agent A DISCARD overturned — wall was LMS artifact. | **INCONCLUSIVE** — accept intact; nudge mild regression source unclear (executor.md confound); wall invalid; idle-LMS re-run needed |
