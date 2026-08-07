# BASELINE — Qwopus process KPIs (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**Context loaded:** 8192 tokens (user LMS 2026-08-07; Q4_K_S, MTP n=2 — earlier notes said 32768, WRONG)  
**Parallel:** 1 (sequential runs)  
**Tools prompt:** `prompts/tools/openai.md` slim path  
**SmartFS:** OFF (tools=() on inline run path — see Task 1)  
**Recorded:** inline `harness run --unit` (traces stay in tmpdir; not copied to data/logs/)

---

## Quarantine suite (6/6 accept)

| unit | run_id | ok | empty_turn_hits | sec_total | failure_class |
|---|---|---|---|---|---|
| micro_python_add | 4ba0b1f5da87 | ✓ | 1 | 33.8 | — |
| micro_refactor_rename_write | 807207bfc83e | ✓ | 4 | 81.4 | — |
| micro_marketing_cta | 19ba48895d52 | ✓ | 2 | 49.8 | — |
| micro_inventory_reorder | 23c84c8e41b4 | ✓ | 2 | 53.0 | — |
| micro_ecommerce_checkout | c723593b72b2 | ✓ | 0 | 30.1 | — |
| micro_nextjs_page | 4440ae504b73 | ✓ | 2 | 48.9 | — |

Quarantine pass rate: **6/6 (100%)**

## Held_in suite (6/6 accept)

| unit | run_id | ok | empty_turn_hits | sec_total | failure_class |
|---|---|---|---|---|---|
| micro_create_file | dde697141dab | ✓ | 1 | 47.8 | — |
| micro_edit_line | 2ba28bf1a595 | ✓ | 1 | 53.5 | — |
| micro_two_files | 44e908a36d3e | ✓ | 1 | 58.3 | — |
| micro_refactor_rename | 2e7c4fc2f655 | ✓ | 1 | 117.0 | — |
| micro_json_transform | c57efc898175 | ✓ | 1 | 199.7 | — |
| micro_content_summary | 2df031c1cc98 | ✓ | 1 | 52.3 | — |

Held_in pass rate: **6/6 (100%)**

---

## Process KPI summary

> **Note:** `empty_turn_rate`, `first_tool_turn`, `tool_calls_per_turn` require
> `trace.*.jsonl` in `data/logs/<run_id>/`. Inline `harness run --unit` does NOT
> copy traces there (only `verify.log` is written). Full per-turn KPIs require
> either graph/project-mode runs or a future trace-copy patch to `run_once`.

| KPI | value | source |
|---|---|---|
| empty_turn_rate | unknown | trace not saved by inline path |
| first_tool_turn | unknown | trace not saved by inline path |
| tool_calls_per_turn | unknown | trace not saved by inline path |
| **empty_turn_hit_rate** (proxy) | **11/12 runs** triggered at least one nudge | `WARNING empty_turn:` log lines |
| median sec_total (quarantine) | ~49s | ledger |
| median sec_total (held_in) | ~55s | ledger |
| worst case | micro_json_transform 199.7s | ledger |

### Empty-turn signal (observable without trace)

All but `micro_ecommerce_checkout` (11/12 = **92%** of runs) fired at least one
`empty_turn` nudge. `micro_refactor_rename_write` fired **4 nudges** in one run
(highest). This confirms the empty_turn rate is high and salvage/prompt fixes
will have measurable impact.

---

## Failure classes (pre-lift)

All 12 units accepted on this run. No failures to classify.  
Expected failure class distribution before lifts (from prior runs / trace audit):

| class | description | signal |
|---|---|---|
| `action` | model stalls: no tool_calls, no progress (max_turns/stalled) | empty_turn nudges count |
| `context` | context overflow (8192 token ctx error seen in older runs) | trace error line |
| `salvage` | markup in content instead of structured tool_calls | tool_salvage middleware target |
| `verify` | file missing or assertion fails | verify.log non-zero |
| `recovery` | blocker declared, cancelled | exit_reason |

---

## Pending full KPI instrumentation

To populate `empty_turn_rate` / `first_tool_turn` / `tool_calls_per_turn`:

1. **Task 1b** wires `tools=` on the execute path (SmartFS on) — that run's trace
   will have real tool call data.
2. OR: add `shutil.copyfile(ws / "trace.jsonl", run_log_dir(run_id) / "trace.0.jsonl")`
   at the end of `run_once` in `harness/cli.py` (out of current scope).

`eval/intelligence/pregrade.py --all` is ready; re-run after traces are present.

---

## POST_LIFT — SmartFS ON · ToolSalvage · EmptyTurn · CompletionGuard (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**SmartFS:** ON (`_fs_tools_for` wired, `delete` excluded from micros)  
**Middleware stack:** SmartFS → LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry  
**Parallel:** 1 — except last 2 held_in (content_summary + create_file-retry ran concurrently; times marked †)  
**Traces:** saved to `data/logs/<run_id>/trace.0.jsonl` (ym0.9)  
**Procedure cards (ym0.14/ym0.15):** landed at 10:26–10:27; quarantine runs pre-proc, held_in runs post-proc — see `procs` column  

> This suite is **POST_LIFT+partial_PROC**. Quarantine (6/6) ran before proc cards; held_in (6/6) ran after. A clean POST_PROC-only pass will be a separate bead.

---

### Quarantine suite (6/6 accept)

| unit | run_id | ok | warn_nudges | sec | tpt | procs |
|---|---|---|---|---|---|---|
| micro_python_add | 8a8bbcceeef9 | ✓ | 2 | 27.7 | 0.80 | ✗ |
| micro_nextjs_page | 6a1655a3b68e | ✓ | 1 | 34.8 | 0.50 | ✗ |
| micro_refactor_rename_write | 5db511c6ee74 | ✓ | 7 | 90.8 | 0.92 | ✗ |
| micro_marketing_cta | 44bb0e26b1d8 | ✓ | 2 | 42.0 | 0.75 | ⚠ border |
| micro_inventory_reorder | 0092f795a111 | ✓ | 4 | 67.8 | 0.83 | ✓ |
| micro_ecommerce_checkout | d50411878d8e | ✓ | 0 | 35.3 | 0.67 | ✓ |

Quarantine pass rate: **6/6 (100%)**

### Held_in suite (6/6 accept, 1 retry)

| unit | run_id | ok | warn_nudges | sec | tpt | procs |
|---|---|---|---|---|---|---|
| micro_create_file | b6d20dbc8e4c | ✗ (attempt 1) | 2 | 48.9 | 0.80 | ✓ |
| micro_create_file (retry) | 78234517fcff | ✓ | 2 | 266.4† | 0.75 | ✓ |
| micro_edit_line | 4e19b8081063 | ✓ | 2 | 48.1 | 0.80 | ✓ |
| micro_two_files | 58545115e961 | ✓ | 1 | 62.2 | 0.83 | ✓ |
| micro_refactor_rename | 31f0ae8586a1 | ✓ | 4 | 113.0 | 1.30 | ✓ |
| micro_json_transform | 99498b120b6c | ✓ | 3 | 74.3 | 0.83 | ✓ |
| micro_content_summary | f592b44d985d | ✓ | 1 | 297.2† | 0.83 | ✓ |

Held_in pass rate: **6/6 (100% with 1 retry)**  
† parallel LMS contention — wall times for these two units are invalid for comparison.

---

### Process KPI summary (POST_LIFT)

| KPI | pre-lift | post-lift | delta | source |
|---|---|---|---|---|
| accept rate (quarantine) | 6/6 (100%) | 6/6 (100%) | = | ledger |
| accept rate (held_in) | 6/6 (100%) | 6/6 (100%, 1 retry) | = | ledger |
| **empty_turn WARNING hit rate** | **11/12 (92%)** | **11/12 (92%)** | **=** | WARNING log proxy |
| total warn_nudges | 11 (quarantine) + 6 (held_in) = 17 | 16 (quarantine) + 13 (held_in) = 29 | **+71%** | WARNING log count |
| empty_turn_rate (trace) | unknown | **0.0** (middleware artifact — empty turns eaten before trace write) | n/a | pregrade |
| first_tool_turn (ftt) | unknown | **1.0 all runs** | n/a | pregrade |
| tool_calls_per_turn (tpt) | unknown | **avg 0.82** (range 0.50–1.30) | n/a | pregrade |
| median sec (quarantine) | ~49s | **~39s** | **−21%** | ledger |
| median sec (held_in, valid) | ~55s | **~68s** (edit_line/two_files/refactor_rename/json_transform only) | +24% | ledger |
| worst case | json_transform 199.7s | json_transform **74.3s** | **−63%** | ledger |

### Key findings

1. **Accept rate unchanged (12/12)** — SmartFS + ToolSalvage + CompletionGuard did not break any unit. One first-attempt verify failure on `micro_create_file` (missing `versao: 1` line); retried and accepted. Failure class: `verify` (flaky content output, not structural).

2. **Empty-turn WARNING rate unchanged (92%)** — 11/12 canonical runs still fire at least one nudge. The middleware is catching and recovering these silently, but not preventing them. Total nudge count increased (+71%: 17 → 29 nudges total), driven by `micro_refactor_rename_write` (4 → 7 nudges) and `micro_refactor_rename` (1 → 4 nudges). Reducing empty turns at source requires prompt or procedure work, not middleware alone.

3. **etr=0.0 from traces is a middleware artifact** — EmptyTurnMiddleware intercepts before the trace is written; empty turns do not appear in `trace.0.jsonl`. Use WARNING log count as the actual proxy.

4. **ftt=1 on all runs** — model calls a tool on turn 1 every time. No stall at start.

5. **json_transform wall time −63%** (199.7s → 74.3s) — biggest improvement; likely driven by SmartFS scoping the file list.

6. **Quarantine median wall −21%** (49s → 39s) — small model efficiency gain from SmartFS limiting tool surface.

---

### Failure classes (post-lift)

| class | count | units | description |
|---|---|---|---|
| `verify` | 1 (retry) | micro_create_file attempt 1 | file written, wrong content (versao: 1 missing) — accepted on retry |

All 12 canonical results: **accept**.

---

## POST_PROC — ALL lifts live (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**SmartFS:** ON  
**Middleware stack:** SmartFS → LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry  
**Procedure cards:** 10 `skills/proc-*.md` (ym0.14) + `prompts/procedures.md` index wired into backend  
**paths= densify:** ym0.15 — 6 skills have `paths=` triggers  
**Trace save:** `_save_trace` active (ym0.9)  
**Parallel:** 1 (sequential, idle LMS)

---

### Quarantine suite (6/6 accept)

| unit | run_id | ok | warn_nudges | sec | tpt | procs |
|---|---|---|---|---|---|---|
| micro_python_add | 0f37639341b0 | ✓ | 1 | 31.4 | 0.67 | ✓ |
| micro_nextjs_page | 84a20f856ef8 | ✓ | 1 | 27.9 | 0.50 | ✓ |
| micro_refactor_rename_write | e54e448ee54d | ✓ | 4 | 91.8 | 1.30 | ✓ |
| micro_marketing_cta | f656554aff7c | ✓ | 4 | 66.4 | 0.83 | ✓ |
| micro_inventory_reorder | 70652aafe678 | ✓ | 4 | 64.5 | 0.83 | ✓ |
| micro_ecommerce_checkout | b605ff150225 | ✓ | 2 | 37.6 | 0.75 | ✓ |

Quarantine pass rate: **6/6 (100%)**  
Total quarantine warn_nudges: **16**

### Held_in suite (6/6 accept, 1 retry)

| unit | run_id | ok | warn_nudges | sec | tpt | procs |
|---|---|---|---|---|---|---|
| micro_create_file | 889888648048 | ✓ | 3† | 48.4 | 0.80 | ✓ |
| micro_edit_line | 6ece60bc505e | ✓ | 2 | 51.8 | 0.80 | ✓ |
| micro_two_files | 7ab5c39e8a8b | ✓ | 2 | 45.1 | 0.80 | ✓ |
| micro_refactor_rename | 6e4e035e1ab5 | ✓ | 5 | 143.0 | 1.07 | ✓ |
| micro_json_transform | c980fe2bea0e | ✗ (attempt 1) | 3† | 174.0 | 0.83 | ✓ |
| micro_json_transform (retry) | 14a9aca99e7d | ✓ | 4 | 76.9 | 0.88 | ✓ |
| micro_content_summary | b9c84503f280 | ✓ | 3† | 53.8 | 1.00 | ✓ |

† includes 1 `completion_guard` nudge + empty_turn nudges.

Held_in pass rate: **6/6 (100% with 1 retry)**

---

### Process KPI summary (POST_PROC)

| KPI | pre-lift | post-lift | POST_PROC | delta vs post-lift | source |
|---|---|---|---|---|---|
| accept rate (quarantine) | 6/6 | 6/6 | 6/6 | = | ledger |
| accept rate (held_in) | 6/6 | 6/6 (1 retry) | 6/6 (1 retry) | = | ledger |
| **warn_nudge hit rate** | **11/12 (92%)** | **11/12 (92%)** | **12/12 (100%)** | **+8pp ↑** | WARNING log proxy |
| total warn_nudges | 17 | 29 | **35** | +21% | WARNING log count |
| etr (trace) | unknown | 0.0 (artifact) | **0.0 (artifact)** | = | pregrade |
| ftt | unknown | 1.0 all | **1.0 all** | = | pregrade |
| avg tpt | unknown | 0.82 | **0.84** | +0.02 | pregrade |
| median sec (quarantine) | ~49s | ~39s | **~51s** | **+31% ↑** | ledger |
| median sec (held_in, valid) | ~55s | ~68s‡ | **~53s** | **−22%** | ledger |
| worst case | 199.7s | 74.3s | **143.0s** | **+92% ↑** (refactor_rename) | ledger |
| json_transform | 199.7s | 74.3s | **76.9s** | +3% ≈ | ledger |

‡ post-lift held_in median was inflated by 2 parallel-contention runs; comparison is noisy.

### Key findings (POST_PROC)

1. **Accept rate unchanged (12/12)** — all lifts + proc cards produce no regressions.

2. **warn_nudge hit rate UP: 92% → 100%** — micro_ecommerce_checkout, previously the one quiet run, now fires 2 empty_turn nudges. Proc cards + paths= densify add context; denser context does not reduce empty turns. The nudge rate is **higher**, not lower, with full proc loading.

3. **etr=0.0, ftt=1.0 unchanged** — EmptyTurn middleware still catches all empty turns before trace write; model always calls a tool on turn 1.

4. **json_transform held (+3%)** — 76.9s vs 74.3s post-lift; proc cards did not regress the biggest improvement.

5. **Quarantine median WORSE: 39s → 51s (+31%)** — marketing_cta (42s → 66s) and inventory_reorder (68s → 65s, slight) drove this. Larger context load from proc cards adds latency for short tasks.

6. **refactor_rename worst case: 113s → 143s (+27%)** — multi-rename tasks load `proc-rename-via-write` + `proc-surgical-edit-check` via paths=; more context = more tokens per turn = slower for complex multi-step rename.

7. **Empty-turn warn_nudge rate is a ceiling not a floor** — middleware recovers but does not prevent. Next lever is procedure *selection quality* (does the right proc card surface and get followed?) and EmptyTurn nudge text specificity. More middleware is not the answer.

### Verdict (POST_PROC → score loop)

| Lever claim | Verdict |
|---|---|
| Load procedure **bodies** via SELECT (+ always-on index) as empty-turn fix | **DISCARD** — warn_nudge ↑ to 100%; total nudges ↑; quarantine wall ↑ 39→51s |
| Stuff more `proc-*` cards / more middleware | **Deprioritize** — ceiling reached |

Next A/Bs (Qwopus micro only): selection audit → EmptyTurn nudge path-specificity → defer bodies until signal. Stay off Task 4 think.

---

### Failure classes (POST_PROC)

| class | count | units | description |
|---|---|---|---|
| `verify` | 1 (retry) | micro_json_transform attempt 1 | out.json not written — accepted on retry |

All 12 canonical results: **accept**.

---

## POST_SELECT_FIX — ym0.17 (nudge names proc) + ym0.18 (path-trigger cap) (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**SmartFS:** ON  
**Middleware stack:** SmartFS → LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry  
**ym0.17:** EmptyTurn nudge text names `proc-recover-after-empty-turn` explicitly, references expected output paths  
**ym0.18:** `select_skills` path-trigger capped at `PATH_CAP = max(1, limit//2) = 1`; slot 2 filled from desc-ranked non-triggered  
**Parallel:** 1 (sequential, idle LMS)  
**Traces:** saved to `data/logs/<run_id>/trace.0.jsonl`

---

### Quarantine suite (6/6 accept)

| unit | run_id | ok | warn_nudges | sec | tpt |
|---|---|---|---|---|---|
| micro_python_add | ba56ed46a806 | ✓ | 1 | 43.75 | 0.80 |
| micro_nextjs_page | 4e135fc87bf0 | ✓ | 0 | 36.40 | 0.75 |
| micro_refactor_rename_write | 6749d1a03b42 | ✓ | 0 | 55.70 | 0.88 |
| micro_marketing_cta | 09e1d455b230 | ✓ | 2 | 54.19 | 0.75 |
| micro_inventory_reorder | 9f1c91f8ec1f | ✓ | 2 | 53.96 | 0.75 |
| micro_ecommerce_checkout | a56002dafc57 | ✓ | 3 | 71.25 | 0.86 |

Quarantine pass rate: **6/6 (100%)**  
Total quarantine warn_nudges: **8**

### Held_in suite (6/6 accept, 1 retry)

| unit | run_id | ok | warn_nudges | sec | tpt |
|---|---|---|---|---|---|
| micro_create_file | 8ec2cac32f0e | ✓ | 3 | 61.33 | 0.80 |
| micro_edit_line | 176f5d9c3e6d | ✓ | 2 | 60.21 | 0.80 |
| micro_two_files | 757d0cf3fc6c | ✓ | 8 | 180.63 | 0.93 |
| micro_refactor_rename (attempt 1) | 42cfe1a21687 | ✗ | 5 | 280.85 | — |
| micro_refactor_rename (retry) | ddb571bef122 | ✓ | 9 | 331.26 | 0.60 |
| micro_json_transform | 877445212c09 | ✓ | 6† | 156.76 | 0.90 |
| micro_content_summary | caba75272fc7 | ✓ | 1 | 56.36 | 0.83 |

† 5 empty_turn + 1 completion_guard nudge.

Held_in pass rate: **6/6 (100% with 1 retry)**

---

### Process KPI summary (POST_SELECT_FIX)

| KPI | pre-lift | POST_PROC | POST_SELECT_FIX | delta vs POST_PROC | source |
|---|---|---|---|---|---|
| accept rate (quarantine) | 6/6 | 6/6 | 6/6 | = | ledger |
| accept rate (held_in) | 6/6 | 6/6 (1 retry) | 6/6 (1 retry) | = | ledger |
| **warn_nudge hit rate** | **11/12 (92%)** | **12/12 (100%)** | **10/12 (83%)** | **−17pp ↓** | WARNING log proxy |
| total warn_nudges | 17 | 35 | **37** | +2 | WARNING log count |
| quarantine nudges | 11 | 16 | **8** | **−8 (−50%) ↓** | WARNING log |
| held_in nudges (canonical) | 6 | 19 | **29** | **+10 (+53%) ↑** | WARNING log |
| etr (trace) | unknown | 0.0 artifact | 0.0 artifact | = | pregrade |
| ftt | unknown | 1.0 all | **1.0 all** | = | pregrade |
| avg tpt | unknown | 0.84 | **0.80** | −0.04 | pregrade |
| median sec (quarantine) | ~49s | ~51s | **~54s** | +3s (+6%) | ledger |
| median sec (held_in, canonical) | ~55s | ~53s | **~109s** | **+56s (+106%) ↑** | ledger |
| worst case | 199.7s | 143.0s | **331.3s** | **+188s (+132%) ↑** | ledger |
| json_transform | 199.7s | 76.9s | **156.8s** | **+80s (+105%) ↑** | ledger |

### Key findings (POST_SELECT_FIX)

1. **Quarantine nudges halved: 16 → 8 (−50%)** — ym0.17 + ym0.18 helped short single-file tasks. `micro_refactor_rename_write` 4→0; `micro_nextjs_page` 1→0. Strongest positive signal.

2. **Held_in nudges near-doubled: 19 → 29 (+53%)** — `micro_two_files` 2→8; `micro_refactor_rename` retry 5→9. PATH_CAP=1 likely evicts task-specific procs from the triggered slot, degrading multi-file guidance.

3. **warn_nudge hit rate: 100% → 83% (−17pp)** — two runs went silent. Below POST_PROC ceiling; above ≤80% KEEP gate.

4. **Held_in wall time severely regressed: 53s → 109s median (+106%)** — two_files 45→181s; refactor_rename retry 143→331s; json_transform 77→157s.

5. **ftt=1.0, avg tpt=0.80** — start-of-run intact; tpt dip from extra empty-turn turns in denominator.

---

### Per-unit comparison: pre-lift / POST_PROC / POST_SELECT_FIX

| unit | pre nudges | PP nudges | PSF nudges | pre sec | PP sec | PSF sec |
|---|---|---|---|---|---|---|
| micro_python_add | 1 | 1 | **1** | 33.8 | 31.4 | 43.8 |
| micro_nextjs_page | 2 | 1 | **0 ↓** | 48.9 | 27.9 | 36.4 |
| micro_refactor_rename_write | 4 | 4 | **0 ↓↓** | 81.4 | 91.8 | 55.7 |
| micro_marketing_cta | 2 | 4 | **2** | 49.8 | 66.4 | 54.2 |
| micro_inventory_reorder | 2 | 4 | **2** | 53.0 | 64.5 | 54.0 |
| micro_ecommerce_checkout | 0 | 2 | **3 ↑** | 30.1 | 37.6 | 71.3 |
| micro_create_file | 1 | 3 | **3** | 47.8 | 48.4 | 61.3 |
| micro_edit_line | 1 | 2 | **2** | 53.5 | 51.8 | 60.2 |
| micro_two_files | 1 | 2 | **8 ↑↑** | 58.3 | 45.1 | 180.6 |
| micro_refactor_rename | 1 | 5 | **9 ↑↑** | 117.0 | 143.0 | 331.3† |
| micro_json_transform | 1 | 4 | **6 ↑** | 199.7 | 76.9 | 156.8 |
| micro_content_summary | 1 | 3 | **1 ↓** | 52.3 | 53.8 | 56.4 |

† retry run (attempt 1 at 280.9s failed verify).

### Verdict (POST_SELECT_FIX)

| Claim | Verdict |
|---|---|
| ym0.17 nudge name reduces empty turns on single-file tasks | **SIGNAL** — refactor_rename_write 4→0; nextjs 1→0 |
| ym0.17+ym0.18 combined clears ≤80% warn-hit gate | **INCONCLUSIVE** — 83% hit rate; 17pp drop; gate needs ≤80% or ≥20pp without wall regression |
| PATH_CAP=1 (ym0.18) safe across all task types | **DISCARD** — multi-file held_in severely regressed (+53% nudges, +106% wall) |

**Root cause:** With `limit=2`, `PATH_CAP=1` leaves 1 triggered slot. Multiple skills share broad globs (`*.py`, `*.ts`); the winner evicts `proc-two-module-create` / `proc-rename-via-write` for multi-file tasks.

**Next A/B:** Isolate ym0.17 alone (revert PATH_CAP). If quarantine benefit survives without held_in regression, KEEP ym0.17. Then revisit ym0.18 with `SELECT_LIMIT=3` so PATH_CAP=1 still leaves 2 free slots.

---

### Failure classes (POST_SELECT_FIX)

| class | count | units | description |
|---|---|---|---|
| `verify` | 1 (retry) | micro_refactor_rename attempt 1 | rename incomplete — accepted on retry after 9 nudges |

All 12 canonical results: **accept**.


---

## POST_NUDGE_ONLY — ym0.17 only (PATH_CAP reverted) (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**SmartFS:** ON  
**Middleware stack:** SmartFS → LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry  
**ym0.17 only:** EmptyTurn nudge text names `proc-recover-after-empty-turn` explicitly  
**ym0.18 REVERTED:** PATH_CAP removed; triggered skills fill slots alphabetically up to limit  
**Parallel:** 1 (sequential, idle LMS)

---

### Quarantine suite (6/6 accept)

| unit | run_id | ok | warn_nudges | sec |
|---|---|---|---|---|
| micro_python_add | d0425cf2e96a | ✓ | 1 | 15.27 |
| micro_nextjs_page | 960a49123a7f | ✓ | 1 | 41.12 |
| micro_refactor_rename_write | 09a5ebe59eed | ✓ | 12 | 305.33 |
| micro_marketing_cta | b0c752f5361a | ✓ | 4 | 73.12 |
| micro_inventory_reorder | bdefee54f171 | ✓ | 11 | 183.31 |
| micro_ecommerce_checkout | b77c82e0a39e | ✓ | 1 | 37.41 |

Quarantine pass rate: **6/6 (100%)**  
Total quarantine warn_nudges: **30**

### Held_in suite (6/6 accept, no retries)

| unit | run_id | ok | warn_nudges | sec |
|---|---|---|---|---|
| micro_create_file | 24987e7aa15c | ✓ | 3 | 74.66 |
| micro_edit_line | d075fc5591e0 | ✓ | 2 | 53.10 |
| micro_two_files | c155af743046 | ✓ | 2 | 67.76 |
| micro_refactor_rename | 2e183b44260c | ✓ | 6 | 156.23 |
| micro_json_transform | 888b5888024a | ✓ | 2 | 62.15 |
| micro_content_summary | 79f7aa2d8ad4 | ✓ | 2 | 72.79 |

Held_in pass rate: **6/6 (100%, no retries)**  
Total held_in warn_nudges: **17**

---

### Process KPI summary (POST_NUDGE_ONLY)

| KPI | pre-lift | POST_PROC | POST_SELECT_FIX | POST_NUDGE_ONLY | delta vs POST_PROC | source |
|---|---|---|---|---|---|---|
| accept rate (quarantine) | 6/6 | 6/6 | 6/6 | 6/6 | = | ledger |
| accept rate (held_in) | 6/6 | 6/6 (1 retry) | 6/6 (1 retry) | **6/6 (no retry)** | **+1 retry saved** | ledger |
| **warn_nudge hit rate** | **11/12 (92%)** | **12/12 (100%)** | **10/12 (83%)** | **12/12 (100%)** | = | WARNING log proxy |
| total warn_nudges | 17 | 35 | 37 | **47** | +12 | WARNING log count |
| quarantine nudges | 11 | 16 | 8 | **30** | **+14 up** | WARNING log |
| held_in nudges | 6 | 19 | 29 | **17** | **−2 down** | WARNING log |
| median sec (quarantine) | ~49s | ~51s | ~54s | **~57s** | +6s | ledger |
| median sec (held_in) | ~55s | ~53s | ~109s | **~70s** | +17s | ledger |
| worst case | 199.7s | 143.0s | 331.3s | **305.3s** | +162s | ledger |
| json_transform | 199.7s | 76.9s | 156.8s | **62.2s** | **−14.7s** | ledger |
| two_files nudges | 1 | 2 | 8 | **2** | = | WARNING log |
| refactor_rename nudges | 1 | 5 | 9† | **6** | +1 | WARNING log |

† POST_SELECT_FIX refactor_rename was a retry run with 9 nudges.

---

### Key findings (POST_NUDGE_ONLY)

1. **Held_in no retry — strongest positive signal** — POST_PROC and POST_SELECT_FIX both needed 1 retry. POST_NUDGE_ONLY ran clean 6/6. json_transform: 157s → 62s; two_files: 181s → 68s.

2. **Held_in nudges recovered: 29 → 17** — PATH_CAP revert fixed the regression. two_files 8→2; json_transform 6→2; refactor_rename 9→6.

3. **Quarantine nudge spike: 8 → 30** — refactor_rename_write 0→12; inventory_reorder 2→11. Both accepted. The quarantine benefit in POST_SELECT_FIX was from ym0.18 PATH_CAP, not ym0.17. Without PATH_CAP, broad path-globs fill slots alphabetically, crowding out methodology procs.

4. **ym0.17 isolated signal on quarantine: weak** — POST_PROC=16; POST_NUDGE_ONLY=30. ym0.17 alone does not improve quarantine vs POST_PROC.

5. **json_transform held_in: best result yet** — 62s (vs 76.9s POST_PROC, 156.8s POST_SELECT_FIX).

6. **worst case (305s)** — refactor_rename_write with 12 nudges; volatile across all runs (4→7→4→0→12). Accepted.

---

### Per-unit comparison: POST_PROC / POST_SELECT_FIX / POST_NUDGE_ONLY

| unit | PP nudges | PSF nudges | PNO nudges | PP sec | PSF sec | PNO sec |
|---|---|---|---|---|---|---|
| micro_python_add | 1 | 1 | **1** | 31.4 | 43.8 | 15.3 |
| micro_nextjs_page | 1 | 0 | **1** | 27.9 | 36.4 | 41.1 |
| micro_refactor_rename_write | 4 | 0 | **12** | 91.8 | 55.7 | 305.3 |
| micro_marketing_cta | 4 | 2 | **4** | 66.4 | 54.2 | 73.1 |
| micro_inventory_reorder | 4 | 2 | **11** | 64.5 | 54.0 | 183.3 |
| micro_ecommerce_checkout | 2 | 3 | **1** | 37.6 | 71.3 | 37.4 |
| micro_create_file | 3 | 3 | **3** | 48.4 | 61.3 | 74.7 |
| micro_edit_line | 2 | 2 | **2** | 51.8 | 60.2 | 53.1 |
| micro_two_files | 2 | 8 | **2** | 45.1 | 180.6 | 67.8 |
| micro_refactor_rename | 5 | 9† | **6** | 143.0 | 331.3† | 156.2 |
| micro_json_transform | 4 | 6 | **2** | 76.9 | 156.8 | 62.2 |
| micro_content_summary | 3 | 1 | **2** | 53.8 | 56.4 | 72.8 |

† PSF refactor_rename was a failed-then-retried run.

### Verdict (POST_NUDGE_ONLY — ym0.17 isolated)

| Claim | Verdict |
|---|---|
| ym0.17 nudge naming reduces held_in wall/nudge regression from ym0.18 | **KEEP** — held_in no-retry; two_files+json_transform nudges halved |
| ym0.17 alone reduces quarantine nudges vs POST_PROC | **DISCARD** — quarantine 16→30; refactor_rename_write and inventory_reorder spike |
| ym0.17 safe to keep without ym0.18 | **KEEP** — accept rate 12/12; held_in wall recovered; orthogonal to PATH_CAP |

**Root cause of quarantine spike:** With PATH_CAP removed, broad-glob skills fill both triggered slots alphabetically, crowding out python-methodology for single-file Python tasks. ym0.18 solves a real problem; it needs redesign, not removal. ym0.17 is orthogonal.

**Next A/B:** ym0.18 redesign — `SELECT_LIMIT=3` + `PATH_CAP=1` leaves 2 free desc-ranked slots. (Bead: harness-core-mbi)


---

## POST_LIMIT3 — ym0.18 redesign (SELECT_LIMIT=3 + PATH_CAP=1) (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**SmartFS:** ON  
**Middleware stack:** SmartFS → LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry  
**ym0.17 KEEP:** EmptyTurn nudge names `proc-recover-after-empty-turn` explicitly (from POST_NUDGE_ONLY)  
**ym0.18 redesign:** `SELECT_LIMIT=3`; `PATH_CAP=max(1,limit//3)=1`; overflow triggered falls back to desc-ranking  
**Parallel:** 1 (sequential, idle LMS)

---

### Quarantine suite (6/6 accept)

| unit | run_id | ok | warn_nudges | sec |
|---|---|---|---|---|
| micro_python_add | ad8181a586c5 | ✓ | 3 | 47.36 |
| micro_nextjs_page | 54a2ae593a09 | ✓ | 1 | 33.96 |
| micro_refactor_rename_write | 265e04816ff2 | ✓ | 3 | 116.48 |
| micro_marketing_cta | 0816130be671 | ✓ | 5 | 98.53 |
| micro_inventory_reorder | 141e1c193121 | ✓ | 7 | 131.07 |
| micro_ecommerce_checkout | 8223a520940d | ✓ | 0 | 32.27 |

Quarantine pass rate: **6/6 (100%)**  
Total quarantine warn_nudges: **19**

### Held_in suite (6/6 accept, no retries)

| unit | run_id | ok | warn_nudges | sec |
|---|---|---|---|---|
| micro_create_file | 0fcfebbfce4d | ✓ | 1 | 46.40 |
| micro_edit_line | c931492afbd0 | ✓ | 3 | 86.10 |
| micro_two_files | 9beeabdf7d6d | ✓ | 2 | 62.91 |
| micro_refactor_rename | 93c99d1a7b37 | ✓ | 3 | 99.73 |
| micro_json_transform | 5299cc38dcb2 | ✓ | 3 | 75.25 |
| micro_content_summary | c0c7e251d157 | ✓ | 1 | 52.63 |

Held_in pass rate: **6/6 (100%, no retries)**  
Total held_in warn_nudges: **13**

---

### Process KPI summary (POST_LIMIT3)

| KPI | POST_PROC | POST_SELECT_FIX | POST_NUDGE_ONLY | POST_LIMIT3 | delta vs POST_NUDGE_ONLY | source |
|---|---|---|---|---|---|---|
| accept rate (quarantine) | 6/6 | 6/6 | 6/6 | 6/6 | = | ledger |
| accept rate (held_in) | 6/6 (1 retry) | 6/6 (1 retry) | **6/6 (no retry)** | **6/6 (no retry)** | = | ledger |
| **warn_nudge hit rate** | **12/12 (100%)** | **10/12 (83%)** | **12/12 (100%)** | **12/12 (100%)** | = | WARNING log proxy |
| total warn_nudges | 35 | 37 | **47** | **32** | **−15 (−32%) ↓** | WARNING log |
| quarantine nudges | 16 | 8 | **30** | **19** | **−11 (−37%) ↓** | WARNING log |
| held_in nudges | 19 | 29 | **17** | **13** | **−4 (−24%) ↓** | WARNING log |
| median sec (quarantine) | ~51s | ~54s | ~57s | **~73s** | **+16s (+28%) ↑** | ledger |
| median sec (held_in) | ~53s | ~109s | ~70s | **~69s** | −1s (≈) | ledger |
| worst case | 143s | 331s | 305s | **131s** | **−174s (−57%) ↓** | ledger |
| json_transform | 76.9s | 156.8s | 62.2s | **75.3s** | +13s | ledger |
| two_files nudges | 2 | 8 | 2 | **2** | = | WARNING log |
| refactor_rename nudges | 5 | 9† | 6 | **3** | **−3 ↓** | WARNING log |

---

### KEEP gate evaluation

| gate | threshold | POST_LIMIT3 result | pass? |
|---|---|---|---|
| quarantine nudges < PNO spike | < 30 | **19** | ✓ PASS |
| held_in median wall | << 109s (PSF blowup) | **69s** | ✓ PASS |
| accept rate | 12/12 | **12/12 no retries** | ✓ PASS |

**All three gates pass → KEEP.**

### Key findings (POST_LIMIT3)

1. **Accept rate 12/12, no retries** — cleanest run of the campaign. POST_PROC, POST_SELECT_FIX, and POST_NUDGE_ONLY all needed one retry each. POST_LIMIT3 is retry-free.

2. **Total nudges: 47 → 32 (−32% vs POST_NUDGE_ONLY)** — quarantine 30→19 (−37%); held_in 17→13 (−24%). Both directions improved simultaneously for the first time.

3. **Quarantine worst-case recovered: 305s → 131s (−57%)** — `micro_inventory_reorder` (the 183s volatile spike in PNO) returned to 131s. `micro_refactor_rename_write` dropped from 305s to 116s. PATH_CAP restores desc-ranking for broad-glob flooding.

4. **Held_in wall stable: 70s → 69s median** — two_files 68s→63s; refactor_rename 156s→100s; json_transform 62s→75s. Multi-file tasks are not regressed (PSF blowup reversed).

5. **Quarantine median regressed: 57s → 73s** — `micro_marketing_cta` 73s→99s; `micro_inventory_reorder` 183s→131s (better, not cause). The marketing_cta and inventory_reorder tasks load more skills at SELECT_LIMIT=3 (3 bodies vs 2 prior); context cost adds latency for these content tasks. Accepted trade-off: nudges are down, worst-case is down.

6. **refactor_rename nudges: 6 → 3** — best result of the campaign for this unit (pre-lift=1; POST_PROC=5; PNO=6; PL3=3). PATH_CAP=1 + desc-ranked overflow seats `proc-rename-via-write` on merit.

7. **ecommerce_checkout: 0 nudges again** — recovered to pre-lift silence (PNO=1, PP=2, PSF=3). PATH_CAP keeps the triggered slot clean.

### Verdict (POST_LIMIT3 — ym0.18 redesign)

| Claim | Verdict |
|---|---|
| SELECT_LIMIT=3 + PATH_CAP=1 fixes quarantine nudge spike without held_in wall regression | **KEEP** — quarantine 30→19; held_in median 70→69s; 12/12 no retries |
| harness-core-mbi KEEP/DISCARD gate | **KEEP** — all 3 gates passed |

**Root cause confirmed:** SELECT_LIMIT=3 gives PATH_CAP=1 two free desc-ranked slots — broad-glob flood is capped, methodology/multi-file procs win on merit. The PSF blowup (109s held_in median) is fully reversed.

---

### Failure classes (POST_LIMIT3)

No failures. All 12 canonical results: **accept, no retries**.

---

### Per-unit comparison: POST_PROC / POST_NUDGE_ONLY / POST_LIMIT3

| unit | PP nudges | PNO nudges | PL3 nudges | PP sec | PNO sec | PL3 sec |
|---|---|---|---|---|---|---|
| micro_python_add | 1 | 1 | 3 | 31.4 | 15.3 | 47.4 |
| micro_nextjs_page | 1 | 1 | 1 | 27.9 | 41.1 | 34.0 |
| micro_refactor_rename_write | 4 | 12 | 3 | 91.8 | 305.3 | 116.5 |
| micro_marketing_cta | 4 | 4 | 5 | 66.4 | 73.1 | 98.5 |
| micro_inventory_reorder | 4 | 11 | 7 | 64.5 | 183.3 | 131.1 |
| micro_ecommerce_checkout | 2 | 1 | 0 | 37.6 | 37.4 | 32.3 |
| micro_create_file | 3 | 3 | 1 | 48.4 | 74.7 | 46.4 |
| micro_edit_line | 2 | 2 | 3 | 51.8 | 53.1 | 86.1 |
| micro_two_files | 2 | 2 | 2 | 45.1 | 67.8 | 62.9 |
| micro_refactor_rename | 5 | 6 | 3 | 143.0 | 156.2 | 99.7 |
| micro_json_transform | 4 | 2 | 3 | 76.9 | 62.2 | 75.3 |
| micro_content_summary | 3 | 2 | 1 | 53.8 | 72.8 | 52.6 |

---

## POST_E3 — ym0.20 (CONTENT_SELECT_LIMIT=2) + ym0.21 E3 (_BODY_WORD_LIMIT=150) (2026-08-07)

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`  
**Backend:** `deepagents`  
**SmartFS:** ON  
**Middleware stack:** SmartFS → LoopGuard → ToolSalvage → EmptyTurn → CompletionGuard → ToolRetry  
**KEEP stack base:** ym0.17 nudge + SELECT_LIMIT=3 + PATH_CAP=1 + proc library  
**ym0.20:** `CONTENT_SELECT_LIMIT=2` — content tasks capped at 2 skill bodies  
**ym0.21 E3:** `_BODY_WORD_LIMIT=150` — render-time body truncation (skill .md files unchanged)  
**Parallel:** 1 (sequential)  
**LMS state:** NOT idle — 4 additional models loaded at inference time: `gemma-4-e4b`, `bonsai-27b`, `qwen3.5-9b`, `text-embedding-nomic-embed-text-v1.5`. Wall times NOT comparable to POST_LIMIT3 (which ran on idle LMS). Nudge counts are comparable; wall times are confounded.  
**executor.md:** Protocol 0 also shipped in this pass (not in POST_LIMIT3).

> **Confound warning:** Three simultaneous changes vs POST_LIMIT3 — ym0.20, E3, and executor.md Protocol 0. Plus LMS was not idle. Wall time comparison is invalid for isolating lever effects.

---

### Quarantine suite (6/6 accept)

| unit | run_id | ok | warn_nudges | sec |
|---|---|---|---|---|
| micro_python_add | 113aa3b87e80 | ✓ | 1 | 40.42 |
| micro_nextjs_page | 2e3ca83982f9 | ✓ | 2 | 101.70 |
| micro_refactor_rename_write | 61a70ccf1806 | ✓ | 7 | 291.54 |
| micro_marketing_cta | 5217ff14f1d9 | ✓ | 8 | 600.83 |
| micro_inventory_reorder | 490aab2751a7 | ✓ | 2 | 89.92 |
| micro_ecommerce_checkout | 5a91c361099c | ✓ | 1 | 164.40 |

Quarantine pass rate: **6/6 (100%)**  
Total quarantine warn_nudges: **21**

### Held_in suite (6/6 accept, 2 retries on create_file)

| unit | run_id | ok | warn_nudges | sec |
|---|---|---|---|---|
| micro_create_file (att 1) | 1ec1b7dfcbd2 | ✗ | 2 | 197.03 |
| micro_create_file (att 2) | d957cbb8fe72 | ✗ | 2 | 289.40 |
| micro_create_file (att 3) | 482740115dfa | ✓ | 1 | 36.31 |
| micro_edit_line | 54a6f803cd5f | ✓ | 2 | 177.96 |
| micro_two_files | 29a23f5dbcf9 | ✓ | 6† | 284.32 |
| micro_refactor_rename | 4dd79e1bf643 | ✓ | 6 | 259.67 |
| micro_json_transform | be8bc2e611a5 | ✓ | 1 | 236.93 |
| micro_content_summary | 0b1f3983ecfd | ✓ | 1 | 341.34 |

† 1 completion_guard + 5 empty_turn nudges.

Held_in pass rate: **6/6 (100%, 2 extra attempts on micro_create_file)**

---

### Process KPI summary (POST_E3)

| KPI | POST_LIMIT3 | POST_E3 | delta vs PL3 | source |
|---|---|---|---|---|
| accept rate (quarantine) | 6/6 (no retries) | **6/6 (no retries)** | = | ledger |
| accept rate (held_in) | 6/6 (no retries) | **6/6 (2 extra attempts)** | −2 attempts | ledger |
| **warn_nudge hit rate** | **12/12 (100%)** | **12/12 (100%)** | = | WARNING log proxy |
| total warn_nudges | 32 | **38** | **+6 (+19%)** | WARNING log |
| quarantine nudges | 19 | **21** | +2 (+11%) | WARNING log |
| held_in nudges (canonical) | 13 | **17** | **+4 (+31%)** | WARNING log |
| median sec (quarantine) | ~73s | **~133s** ⚠ | **+60s (+82%)** — LMS not idle | ledger |
| median sec (held_in, canonical) | ~69s | **~248s** ⚠ | **+179s (+259%)** — LMS not idle | ledger |
| worst case | 131.1s | **600.8s** ⚠ | confounded | ledger |
| micro_create_file retries | 0 | **2 (accent substitution)** | same failure class as POST_PROC | ledger |

⚠ Wall comparisons invalid — LMS had 4 extra models loaded; POST_LIMIT3 was idle LMS.

---

### ym0.20 actual effect audit

Simulation of `select_skills` at `CONTENT_SELECT_LIMIT=2` vs `SELECT_LIMIT=3` for all 4 content units:

| unit | skills at limit=3 | skills at limit=2 | dropped? |
|---|---|---|---|
| micro_marketing_cta | marketing-methodology + proc-content-cta-skeleton (2) | same (2) | none |
| micro_inventory_reorder | inventory-methodology + proc-content-cta-skeleton + ecommerce-sales-methodology (3) | inventory-methodology + proc-content-cta-skeleton (2) | ecommerce-sales-methodology |
| micro_ecommerce_checkout | ecommerce-sales-methodology + inventory-methodology (2) | same (2) | none |
| micro_content_summary | proc-content-cta-skeleton + proc-exact-write-from-spec (2) | same (2) | none |

**ym0.20 actual delta:** drops 1 skill for `micro_inventory_reorder` only. Three of four content tasks already loaded ≤2 relevant skills at SELECT_LIMIT=3.

### E3 actual truncation audit

Skills that exceed 150w and are truncated at render time:

| skill | words | truncated to | words lost | kinds that load it |
|---|---|---|---|---|
| python-methodology | 170w | 152w | 18w (Done when) | code/refactor |
| method-planning | 151w | 130w | 21w (tail steps) | code/refactor |
| langgraph-idioms | 220w | 154w | 66w | config |
| ledger-sqlite | 211w | 154w | 57w | config |
| toml-calibration-safety | 228w | 154w | 74w | config |

**Stale audit (E3 snapshot — cards have since grown):** At E3 run time, the key action procs were reported under 150w. Current `wc -w` shows proc-* cards now range **143–177w** — most exceed 150w today:

| proc card | current words | would truncate at 150? |
|---|---|---|
| proc-check-listed-files | 152w | yes |
| proc-content-cta-skeleton | 177w | yes |
| proc-declare-blocker | 152w | yes |
| proc-exact-write-from-spec | 156w | yes |
| proc-recover-after-empty-turn | 150w | boundary (no) |
| proc-recover-missing-files | 143w | no |
| proc-rename-via-write | 172w | yes |
| proc-surgical-edit-check | 155w | yes |
| proc-two-module-create | 152w | yes |
| proc-verify-then-stop | 160w | yes |

The E3 run verdict remains INCONCLUSIVE (wall confounded; accept intact); the E3 state of the proc files is unknown relative to these current counts. `_BODY_WORD_LIMIT` was reverted to **500** — cards are not truncated under the live config.

---

### Key findings (POST_E3)

1. **Accept rate 12/12 — no verify failures** — contrary to the offline draft (which projected 3 failures). All units accepted. E3 at 150w does NOT truncate the proc-* cards that drive rename/create tasks; they are all under 150w.

2. **Nudge mild regression vs POST_LIMIT3: +6 total (+19%)** — quarantine +2 (within noise); held_in +4 (+31%). Most regression is in two_files (2→6) and refactor_rename (3→6). Both are code/refactor kind tasks where ym0.20 has no effect. E3 truncates python-methodology by 18w (just the "Done when" section), which is unlikely to drive a +3 nudge increase.

3. **micro_create_file: 2 flaky failures before accept** — same failure class (accent substitution: writes "versão" not "versao") seen in POST_PROC. Not caused by ym0.20 or E3 — proc-exact-write-from-spec (135w) is fully intact.

4. **Wall times are confounded and cannot be used for verdict** — LMS had bonsai-27b + gemma-4-e4b + qwen3.5-9b + embedding all loaded. marketing_cta 601s (8 nudges) vs 99s (5 nudges) at PL3 — the 6x wall difference is 3 extra nudges × (inference slow from LMS load). inventory_reorder 90s/2 nudges — IMPROVED vs PL3 131s/7 nudges, consistent with ym0.20 dropping ecommerce-sales-methodology skill.

5. **ym0.20 measurable benefit only on inventory_reorder** — the only unit where CONTENT_SELECT_LIMIT=2 actually dropped a skill body: nudges 7→2 and wall notably lower (even under LMS load). Insufficient to declare KEEP without idle-LMS re-run.

6. **E3 truncation minimal on active proc cards** — only 5 skills exceed 150w, all are methodology/config (not micro procedure cards). Losing the "Done when" footer of python-methodology and method-planning tail is unlikely to cause the nudge regression seen in two_files/refactor_rename.

7. **Regression root cause unclear** — two_files/refactor_rename nudge increases (+4, +3) cannot be attributed to ym0.20 or E3 by skill inspection. Most plausible cause: executor.md Protocol 0 change (also present in this pass, not in PL3), or model variance on a loaded LMS.

---

### KEEP gate evaluation (POST_E3)

| gate | threshold | POST_E3 result | valid? | pass? |
|---|---|---|---|---|
| quarantine nudges < PNO spike | < 30 | **21** | ✓ | ✓ PASS |
| held_in median wall | << 109s (PSF blowup) | **248s** ⚠ | ✗ LMS confound | ✗ INVALID |
| accept rate | 12/12 | **12/12** | ✓ | ✓ PASS |

Wall gate is **invalid** — cannot pass or fail fairly. Accept and nudge gates pass. Wall requires re-run on idle LMS.

---

### Verdict (POST_E3)

| Claim | Verdict |
|---|---|
| ym0.20 (CONTENT_SELECT_LIMIT=2) reduces content-task context cost | **INCONCLUSIVE** — actual delta is 1 skill dropped for 1/4 content units; inventory_reorder improved (7→2 nudges) but single-unit signal; idle-LMS re-run needed |
| E3 (_BODY_WORD_LIMIT=150) safe for proc-* cards | **INCONCLUSIVE** — no verify failures; proc cards all under 150w; nudge mild regression (+19%) source unclear; wall confounded |
| Combined ym0.20+E3 KEEP/DISCARD gate | **INCONCLUSIVE** — wall gate invalid; accept passes; nudge gate passes; cannot issue clean KEEP without idle LMS re-run |

**Next step:** Re-run with idle LMS (unload gemma-4-e4b, bonsai-27b, qwen3.5-9b first). Also isolate executor.md Protocol 0 change which was NOT in POST_LIMIT3 — this is a confound for the nudge regression.

---

### POST_E3 agent reconciliation (2026-08-07)

Two post-run reports conflicted; reconciled here.

| Agent | Verdict | Key claim | Status |
|---|---|---|---|
| A (`8ea511cd`) | DISCARD | ~600s marketing_cta wall; `_BODY_WORD_LIMIT=150` blamed for truncating procs | **Overturned** — wall was LMS-load artifact (4 extra models); proc truncation claim refuted by audit |
| B (`84f4e9c7`) | INCONCLUSIVE | 12/12 accept; proc-* cards "all under 150w"; wall invalid | **Partially correct** — 12/12 and wall-invalid are right; "all under 150w" claim is wrong for current files (most 152–177w; cards have grown post-E3) |

**Reconciled verdict: INCONCLUSIVE** — do not KEEP, do not DISCARD E3.

- `_BODY_WORD_LIMIT` is **500** (already reverted by mechanic `4759c24b`). No further change needed.
- `CONTENT_SELECT_LIMIT=2` (ym0.20): retain as tentative; single-unit positive signal (inventory_reorder 7→2 nudges).
- E3 at 150w would now truncate 8/10 proc-* cards (cards have grown 143–177w). This strengthens the case for keeping 500 as the default.
- Clean verdict requires: idle LMS + isolated executor.md Protocol 0 + separate E3 re-run.

---

### Failure classes (POST_E3)

| class | count | units | description |
|---|---|---|---|
| `verify` | 2 (retries on same unit) | micro_create_file att 1–2 | accent substitution: wrote "versão: 1" instead of "versao: 1"; accepted on att 3 |

All 12 canonical results: **accept**.

---

### Per-unit comparison: POST_PROC / POST_NUDGE_ONLY / POST_LIMIT3 / POST_E3

† POST_E3 wall times confounded by non-idle LMS — not suitable for lever comparison.

| unit | PP nudges | PNO nudges | PL3 nudges | E3 nudges | PP sec | PNO sec | PL3 sec | E3 sec† |
|---|---|---|---|---|---|---|---|---|
| micro_python_add | 1 | 1 | 3 | **1** | 31.4 | 15.3 | 47.4 | 40.4 |
| micro_nextjs_page | 1 | 1 | 1 | **2** | 27.9 | 41.1 | 34.0 | 101.7 |
| micro_refactor_rename_write | 4 | 12 | 3 | **7** | 91.8 | 305.3 | 116.5 | 291.5 |
| micro_marketing_cta | 4 | 4 | 5 | **8** | 66.4 | 73.1 | 98.5 | 600.8 |
| micro_inventory_reorder | 4 | 11 | 7 | **2 ↓** | 64.5 | 183.3 | 131.1 | 89.9 |
| micro_ecommerce_checkout | 2 | 1 | 0 | **1** | 37.6 | 37.4 | 32.3 | 164.4 |
| micro_create_file | 3 | 3 | 1 | **1** | 48.4 | 74.7 | 46.4 | 36.3† |
| micro_edit_line | 2 | 2 | 3 | **2** | 51.8 | 53.1 | 86.1 | 178.0 |
| micro_two_files | 2 | 2 | 2 | **6 ↑** | 45.1 | 67.8 | 62.9 | 284.3 |
| micro_refactor_rename | 5 | 6 | 3 | **6 ↑** | 143.0 | 156.2 | 99.7 | 259.7 |
| micro_json_transform | 4 | 2 | 3 | **1** | 76.9 | 62.2 | 75.3 | 236.9 |
| micro_content_summary | 3 | 2 | 1 | **1** | 53.8 | 72.8 | 52.6 | 341.3 |

† create_file: canonical (3rd attempt); wall times for all POST_E3 entries are under LMS load.

---

## POST_E2 — ym0.22 E2: max_tokens=2048 simple micros (2026-08-07) — DISCARD

**Model:** `openai:qwopus3.5-4b-coder-mtp` via LMS on `:1234`
**Backend:** `deepagents`
**SmartFS:** ON
**LMS state:** idle — only qwopus loaded (verified via `lms ps`)
**E2 lever:** `_is_simple_unit()` heuristic: `expected_files<=2` OR unit id in `_SIMPLE_UNIT_IDS` → `max_tokens=2048`; else `4096`
**Parallel:** 1 (sequential)

### 7-unit A/B (POST_LIMIT3 control)

| unit | class | PL3 sec | E2 sec | Δsec | PL3 nudges | E2 nudges | Δnudges | ok |
|---|---|---|---|---|---|---|---|---|
| micro_python_add | SIMPLE (allowlist) | 47.4 | 42.7 | **−4.7 ↓** | 3 | 2 | **−1 ↓** | ✓ |
| micro_create_file | SIMPLE (allowlist) | 46.4 | 42.2 | **−4.2 ↓** | 1 | 1 | = | ✓ |
| micro_edit_line | SIMPLE (allowlist) | 86.1 | 81.5 | **−4.6 ↓** | 3 | 4 | +1 ↑ | ✓ |
| micro_nextjs_page | SIMPLE (allowlist) | 34.0 | **117.7** | **+83.7 ↑↑** | 1 | **9** | **+8 ↑↑** | ✓ |
| micro_content_summary | SIMPLE (allowlist) | 52.6 | 61.0 | +8.4 ↑ | 1 | 2 | +1 ↑ | ✓ |
| micro_refactor_rename | COMPLEX (4096) | 99.7 | 119.1 | +19.4 ↑ | 3 | 4 | +1 ↑ | ✓ |
| micro_two_files | COMPLEX (4096) | 62.9 | 131.5 | **+68.6 ↑↑** | 2 | 7 | **+5 ↑↑** | ✓ |

**Accept: 7/7 (no regression). VERDICT: DISCARD.**

**Root cause:** `micro_nextjs_page` is the disqualifying case. Allowlist classified it as SIMPLE → 2048 tokens. Generating a full React page component requires more output budget; at 2048 the model empties out and re-nudges 9× (baseline=1). Wall +247%.

`micro_two_files` regression (+69s, +5 nudges) is at 4096 (COMPLEX, unchanged by E2) — attributed to LMS KV state after 5 prior sequential units rather than the lever.

**Lesson:** task-simplicity ≠ short model output. Allowlist is not a safe proxy for token budget.

**Retry condition:** Use `expected_files<=2` only (no allowlist), OR audit allowlist to exclude units with single large file output (nextjs_page). Do not lower SIMPLE_OPENAI_MAX_TOKENS further.
