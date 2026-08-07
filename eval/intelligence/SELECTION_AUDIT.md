# SELECTION_AUDIT — ym0.16 findings (2026-08-07)

## TL;DR

The POST_PROC regression (warn_nudge 92% → 100%, q median +12s) is caused by
**path-trigger flooding**, not global-proc crowding or body-load token burn.
`proc-exact-write-from-spec` and `proc-rename-via-write` both declare
`paths = ["**/*.py", "*.py"]`, so **every Python micro that names a .py file
in its prompt fires both triggers simultaneously**, filling all 2 slots before
the ranking runs. The correct cards (`python-methodology`,
`proc-two-module-create`, `proc-surgical-edit-check`) are never selected.

---

## Method

Offline simulation with `harness/skills/loader.py`: enumerate skills, run
`select_skills` for 11 representative micro prompts with realistic `kind` and
`files` (extracted via `PROMPT_FILE_RE`), print triggered vs. ranked vs. crowded.
No LMS required; deterministic. Added `_log.debug` instrumentation to
`select_skills` so live runs now emit crowding evidence at DEBUG level.

---

## Findings

### 1. Path-trigger flood (root cause)

Any prompt that mentions a `.py` file (e.g. `"Write to solution.py"`) fires six
path-triggered skills:

```
proc-exact-write-from-spec   paths=["**/*.py","*.py"]
proc-rename-via-write         paths=["**/*.py","*.py"]
proc-surgical-edit-check      paths=["**/*.py","*.py"]
proc-two-module-create        paths=["**/*.py","*.py"]
python-fixes                  paths=["**/*.py","*.py"]
python-methodology            paths=["**/*.py","*.py","**/fixtures/*.json","out.json"]
```

`triggered + resto` fills SELECT_LIMIT=2 with the **first two triggers in
load order** (alphabetical: `proc-exact-write-from-spec`,
`proc-rename-via-write`). The rest are crowded out.

| micro | files in prompt | selected | crowded out |
|---|---|---|---|
| micro_python_add | solution.py | exact-write, rename-via-write | **python-methodology**, two-module-create, surgical-edit, python-fixes |
| micro_create_file | config.py | exact-write, rename-via-write | **python-methodology**, two-module-create, surgical-edit, python-fixes |
| micro_two_files | main.py, utils.py | exact-write, rename-via-write | **proc-two-module-create**, python-methodology, surgical-edit |
| micro_edit_line | utils.py | exact-write, rename-via-write | **proc-surgical-edit-check**, python-methodology, python-fixes |
| micro_refactor_rename | utils.py | exact-write, rename-via-write | **proc-surgical-edit-check**, python-methodology |

`proc-rename-via-write` fires for `micro_python_add`, `micro_create_file`,
`micro_edit_line` — tasks where rename guidance is noise, not signal.

### 2. Recovery procs do NOT crowd — they never win

Global procs (`kinds = []`, no `paths`):
`proc-recover-after-empty-turn`, `proc-recover-missing-files`,
`proc-check-listed-files`, `proc-declare-blocker`, `proc-verify-then-stop`
have **zero description-token overlap** with typical task prompts, so they are
dropped by the scoring filter when any other skill scores. They do NOT consume
slots. The hypothesis about global-recovery crowding was **wrong**.

### 3. Always-on token budget is acceptable

| component | words |
|---|---|
| executor.md | 426 |
| procedures.md index | 149 |
| 2 × skill body (avg ~135w each) | ~270 |
| **total always-on + skills** | **~845** |

Body load is not the problem at current SELECT_LIMIT=2 + 500w body cap.
For Qwopus with ~8k practical context, ~850w always-on is fine.

### 4. Content micros are healthy

Micros without `.py` files in the prompt use ranking only:

| micro | selected |
|---|---|
| micro_ecommerce_checkout | proc-content-cta-skeleton, ecommerce-sales-methodology ✓ |
| micro_content_summary | proc-content-cta-skeleton, marketing-methodology ✓ |
| micro_marketing_cta | marketing-methodology, proc-content-cta-skeleton ✓ |
| micro_nextjs_page | nextjs-methodology, proc-two-module-create ✓ |
| micro_json_transform | python-methodology, ledger-sqlite (ledger is noise but harmless) |

### 5. Why empty-turn got worse

`proc-recover-after-empty-turn` never gets selected (explained above — no path
and 0 description hits). So when EmptyTurn middleware fires its nudge, the
model has NO recovery procedure in context to follow. The nudge fires into a
context that has `proc-rename-via-write` (wrong card) instead. The recovery
path is broken not because the proc is absent from the library, but because
path-flood prevents it from ever being selected even when ranked.

---

## Logging added (ym0.16)

`harness/skills/loader.py` — `select_skills` now emits at `DEBUG`:
- `select_skills kind= limit= triggered= total_matched= selected=[…]`
- `select_skills PATH-FLOOD: N triggers fill all K slots; crowded_out=[…]`

Fail-open (try/except). Zero behavior change.

---

## Recommendation

### ym0.17 — EmptyTurn nudge path (one-shot A/B) [UNBLOCK NOW]

The nudge text can explicitly name `proc-recover-after-empty-turn` or
`proc-recover-missing-files` by name in the nudge message, so the model finds
the procedure even when it's not in the skill block. This is a text change to
`harness/backends/empty_turn.py` (or equivalent nudge template). Zero ranking
change required. Expected: partial win on warn_nudge.

### ym0.18 — Fix path-trigger flooding [HIGHER IMPACT]

Two options (either or both):

**Option A — Narrow path globs per card (recommended):**
- `proc-rename-via-write`: add `rename` keyword to description so ranking
  favors it only for rename prompts. Remove `*.py` glob or scope it to
  `**/rename_*.py` — path glob should only fire when the file itself signals
  a rename operation.
- `proc-exact-write-from-spec`: keep `*.py` glob but only if prompt contains
  EXACT/EXATAMENTE (check in `_path_hit` or restrict glob scope).
- `proc-surgical-edit-check`: glob is correct but should NOT win over
  `proc-exact-write-from-spec` for new-file tasks.

**Option B — Limit path-trigger to 1 slot (simplest):**
In `select_skills`, cap `triggered` at 1 before appending `resto`:
```python
triggered = triggered[:1]  # one path-trigger max; ranking fills the rest
```
This guarantees at least 1 ranking slot and forces `proc-exact-write-from-spec`
(or `proc-rename-via-write`) to compete against `python-methodology`, not
monopolize both slots.

**Preferred:** Option B for ym0.18 (one-line change, low risk, immediately
unblocks `python-methodology` + `proc-two-module-create`), then clean up globs
in ym0.19 once A/B confirms the improvement.

---

## Issues to unlock

- **ym0.17**: EmptyTurn nudge → name the recovery proc explicitly in nudge text
- **ym0.18**: Fix path-trigger flood (Option B: cap triggered[:1])

---

## Fix applied: ym0.18 path-cap (2026-08-07)

**Root cause confirmed** (ym0.16): 6 skills share `paths=["**/*.py","*.py"]`.
With SELECT_LIMIT=2, `triggered + resto)[:2]` always returned the first two
alphabetically (`proc-exact-write-from-spec`, `proc-rename-via-write`),
crowding out `python-methodology` and task-specific procs.

**Fix implemented** in `harness/skills/loader.py` `select_skills`:
among path-triggered skills, rank by description score and take at most
`PATH_CAP = max(1, limit // 2)` (= 1 for the default limit=2). Fill remaining
slots from non-triggered by score. This preserves the path-trigger mechanism
while ensuring the most query-relevant triggered skill wins the slot.

**Selection matrix after fix (SELECT_LIMIT=2):**

| Query | Files | Selected |
|---|---|---|
| fix bug python service | service.py | python-fixes, proc-verify-then-stop |
| python pytest typed uv run tests | tests/test_foo.py | python-methodology, proc-verify-then-stop |
| rename symbol across files | pedido.py relatorio.py | proc-rename-via-write, method-planning |
| write new file from spec exact content | new_mod.py | proc-exact-write-from-spec, proc-recover-missing-files |
| create module and importer | main.py | proc-two-module-create, dream-code-traceback-most-recent |

Before fix: all .py queries returned `[proc-exact-write-from-spec, proc-rename-via-write]`.

**Tests added:** `test_path_cap_prevents_path_flood` in `tests/test_skills.py`.
28/28 tests pass.


---

## ym0.18 redesign: SELECT_LIMIT=3 + PATH_CAP=max(1,limit//3) (2026-08-07)

**Bead:** `harness-core-mbi`

**Change:** `SELECT_LIMIT=3`; `PATH_CAP = max(1, limit // 3)` (= 1 at default limit=3).  
Triggered skills ranked by description score; top PATH_CAP kept; overflow falls back into resto for desc-ranking.  
Result: 1 best path-triggered + up to 2 desc-ranked remainder (including overflow triggered that lose the triggered slot).

**Offline selection matrix (SELECT_LIMIT=3, PATH_CAP=1):**

| query | files | selected |
|---|---|---|
| fix bug python service | service.py | python-fixes *(triggered)* + python-methodology |
| python pytest typed uv run tests | tests/test_foo.py | python-methodology *(triggered)* + proc-verify-then-stop + python-fixes |
| rename symbol across files | pedido.py relatorio.py | proc-rename-via-write *(triggered)* + method-planning + python-methodology |
| write new file from spec exact content | new_mod.py | proc-exact-write-from-spec *(triggered)* + proc-recover-missing-files + proc-two-module-create |
| create two modules main importer | main.py utils.py | proc-two-module-create *(triggered)* + python-methodology |
| edit single line change function | utils.py | proc-rename-via-write *(triggered)* + method-planning + proc-surgical-edit-check |
| marketing call to action content ecommerce | -- | proc-content-cta-skeleton + ecommerce-sales-methodology + proc-exact-write-from-spec |
| content summary marketing blog post | -- | proc-content-cta-skeleton + marketing-methodology + proc-exact-write-from-spec |
| ecommerce checkout product description | -- | ecommerce-sales-methodology + inventory-methodology |
| nextjs page component typescript react | page.tsx | nextjs-methodology |
| json transform python out.json fixtures | out.json | python-methodology *(triggered via out.json)* + ledger-sqlite + python-fixes |

**Key improvement vs old ym0.18 (PATH_CAP=1@limit=2):**  
Old: 1 triggered + 1 free slot — multi-file tasks only got proc OR methodology, not both.  
New: 1 triggered + 2 free slots — multi-file rename gets `proc-rename-via-write + method-planning + python-methodology`; two-module-create gets `proc-two-module-create + python-methodology`.  

**Flood prevention confirmed:**  
Before (no PATH_CAP): `micro_python_add / service.py` → `proc-exact-write-from-spec, proc-rename-via-write` (alphabetical flood).  
After: `python-fixes, python-methodology` (desc-ranked, correct).
