# MICRO_PROCEDURES — SLM situation-procedure library design
> Filed under `ym0.13` · 2026-08-07 · design only; ym0.14 authors v1 cards

---

## 1. Inventory: what exists today

### 1.1 Always-on prompts (~1 300 words loaded every run)

| file | scope | what it covers | what it misses |
|---|---|---|---|
| `prompts/executor.md` | system | Protocol 0 direct-write gate; ambiguous→plan; planner call; todo discipline; diff-review; verify mandate | Recovery step-by-step; declare-blocker; when each tool is right; fat by design but already compact |
| `prompts/tools/openai.md` | tools | ls/read/write/edit/glob/grep/execute/delete/task reference; virtual path conventions | Situation-specific usage discipline (when to write_file vs edit_file for a rename) |
| `prompts/tools/openai_qwopus3.5-4b-coder-mtp.md` | tools (Qwopus) | Same as above + extra Qwopus path caution; rename turn-discipline | Mostly duplication; path clarification is Qwopus-only nuance |

**Always-on word budget (estimated):** executor ~600 w · tools ~500 w · total ≈ **1 100 w** before any skill.

### 1.2 Selectable skills (loaded by keyword/kind match, SELECT_LIMIT=2, 500 w cap)

| skill | kinds | ~words | what it covers | situation class |
|---|---|---|---|---|
| `python-methodology.md` | code, refactor | ~230 | write/rename/two-module/pytest/json | action (blended) |
| `python-fixes.md` | code | ~80 | diff-min bug fix + verify mandate | action/verify |
| `nextjs-methodology.md` | code | ~150 | App Router page, Server Components, mutations | action (domain) |
| `ecommerce-sales-methodology.md` | content | ~120 | funnel, cart, checkout, payment | domain methodology |
| `inventory-methodology.md` | content | ~110 | SKU, BOM, reorder, idempotent updates | domain methodology |
| `marketing-methodology.md` | content | ~90 | audience, CTA, claims discipline | domain methodology |
| `method-planning.md` | (none) | ~130 | plan skeleton, 6-step cap, risk callout | context |
| `method-research.md` | (none) | ~100 | fact/inference discipline, counter-example | context |
| `langgraph-idioms.md` | code | ~220 | idempotency keys, configurable, interrupt | harness/code |
| `ledger-sqlite.md` | code, infra | ~200 | schema-idempotent, natural-key insert, payload column | harness/code |
| `config-calibration.md` | config | ~60 | TOML delta-small, roundtrip-check | action/config |
| `toml-calibration-safety.md` | config | ~200 | atomic write, fail-open read, genome respect | action/config |
| `dream-code-traceback-most-recent.md` | code | ~120 | episodic: traceback-most-recent signature | recovery (domain) |

**13 skills · ~1 820 w total · none is a pure situation/procedure card**

### 1.3 Recovery middleware (always-on in harness, NOT in prompts — model sees nudge text only)

| middleware | trigger | model sees |
|---|---|---|
| `EmptyTurnMiddleware` | empty content + no tools | nudge: "Continue AGORA: use write_file/edit_file…" |
| `ToolSalvage` | tool markup in content field | transparent (re-routes silently) |
| `CompletionGuard` | stop + expected files missing | nudge: "arquivos esperados não existem: [list]" |

**Gap:** middleware nudges are generic prose. When nudge fires → model has no named procedure to fall back on. The recovery step-by-step lives nowhere selectable.

---

## 2. Situation coverage vs failure class

```
coverage  ✓=covered  ≈=partial  ✗=gap
```

### action
| situation | current coverage | notes |
|---|---|---|
| EXACT write (new file from spec) | ≈ executor Protocol 0 | in always-on but no standalone card with examples |
| rename via write_file (small .py) | ≈ python-methodology | blended with 8 other rules; hard to isolate for SLM |
| two-module create (importer+imported) | ≈ python-methodology | same blended skill |
| surgical edit_file (old_string unique) | ✗ | no card: how to confirm uniqueness, what to do on fail |
| nextjs page EXACT (App Router) | ≈ nextjs-methodology | domain card, not micro action card |
| content skeleton from spec (CTA) | ✗ | marketing has principles, not a skeleton procedure |
| json-transform micro (read fixture → compute → write out.json) | ≈ python-methodology | partial mention |

### recovery
| situation | current coverage | notes |
|---|---|---|
| after empty-turn nudge | ✗ | EmptyTurn fires; no named procedure card |
| after CompletionGuard nudge (missing files) | ✗ | nudge lists files; no card: "do THIS next" |
| after SmartFS read-gate (write refused, re-read) | ✗ | no card anywhere |
| after ToolSalvage (malformed call silently fixed) | n/a | transparent — model need not know |
| declare-blocker when genuinely stuck | ✗ | AGENTS.md mentions `declare_blocker` but not in model context |
| salvage-aware retry (if 2 empty turns → stop) | ✗ | no card; model could loop |

### verify
| situation | current coverage | notes |
|---|---|---|
| verify-then-stop (run verify_cmd, paste output, stop) | ≈ executor + python-fixes | mandate in executor; no standalone step-by-step card |
| check listed files exist before claiming done | ✗ | CompletionGuard catches it but model has no proactive card |
| verify_cmd absent (no command given) | ✗ | no protocol: what to do when verify is undefined |

### context
| situation | current coverage | notes |
|---|---|---|
| skip planner on Protocol 0 | ≈ executor | inline in always-on; not a separately triggerable card |
| slim-tools-only (read before write, no extra tools) | ≈ tools prompt | discipline in tools prompt; no selectable card |
| two-pass read (read → note → edit, not read → edit blindly) | ✗ | read-before-edit is mentioned in AGENTS.md but not as a model-facing card |
| multi-file order discipline (dependency order) | ✗ | method-planning touches it abstractly |

---

## 3. Gap list — ranked by Qwopus leverage

Ranking criteria: frequency of observed failure class × context-save ratio × breadth of tasks it helps.

| rank | name | class | why high leverage |
|---|---|---|---|
| **1** | `recover-after-empty-turn` | recovery | EmptyTurn is the single highest-fire middleware (11/12 pre-lift); when nudge fires, model has no procedure — it loops or stops |
| **2** | `verify-then-stop` | verify | Every micro needs it; failure = false-green claim; currently only a mandate, not a procedure card with step-by-step |
| **3** | `exact-write-from-spec` | action | Protocol 0 is the most common fast path; a standalone card with the 3-step sequence reduces executor.md reliance |
| **4** | `recover-missing-files` | recovery | CompletionGuard fires often (second middleware); model needs "re-read task, write the listed file" — not generic prose |
| **5** | `rename-via-write` | action | Rename is highest error-surface action for SLMs (partial rename, leaving old symbol) |
| **6** | `two-module-create` | action | Python two-file tasks fail when model writes only one module; needs a card that says "write BOTH before stopping" |
| **7** | `surgical-edit-check` | action | edit_file fails silently when old_string not unique; card: confirm uniqueness with grep first |
| **8** | `declare-blocker` | recovery | Model loops when stuck; no card for "stop, name the blocker, do not retry" |
| **9** | `check-listed-files` | verify | Pre-stop checklist: grep/ls for every path in spec before claiming done |
| **10** | `content-cta-skeleton` | action | Content tasks (marketing/inventory/ecommerce) need a 4-step skeleton card so domain methodology skill is not loaded just for structure |

**Next tier (ym0.14 v1.1):** `skip-planner-protocol0` (context), `read-before-edit` (context), `json-micro` (action), `after-read-gate` (recovery).

---

## 4. Chaining design

### 4.1 Context budget (Qwopus, ~8k practical limit)

```
always-on (every turn)
  executor.md          ~600 w    (~750 tok)
  tools/openai.md      ~500 w    (~625 tok)
  procedure index      ~200 w    (~250 tok)   ← NEW: titles + 1-line trigger only
  ─────────────────────────────────────────
  always-on subtotal  ~1 300 w  (~1 625 tok)

per-turn (situation match, ≤ SELECT_LIMIT=2)
  2 × 500 w bodies    ~1 000 w  (~1 250 tok)
  ─────────────────────────────────────────
  prompt overhead     ~2 300 w  (~2 875 tok)

leaves for conversation + task
  8 000 tok − 2 875  = ~5 125 tok             ← comfortable for micros
```

### 4.2 Index card (always-on, lives in a new `prompts/procedures.md`)

```markdown
# Procedure index
Load body when situation matches. ≤2 at a time.

| name | trigger |
|---|---|
| recover-after-empty-turn | nudge fires with "[empty_turn]" |
| verify-then-stop | about to claim task done |
| exact-write-from-spec | EXACT/EXATAMENTE + new file in spec |
| recover-missing-files | nudge fires with "[completion_guard]" |
| rename-via-write | rename symbol across listed .py paths |
| two-module-create | write importer + imported .py together |
| surgical-edit-check | edit_file on existing file |
| declare-blocker | stuck ≥2 attempts, no forward progress |
| check-listed-files | verify step, paths listed in spec |
| content-cta-skeleton | CTA / marketing / inventory content task |
```

**Index size: ~200 w — fits always-on without touching executor.md.**

### 4.3 Load sequence (one turn)

```
1. Middleware injects nudge text  (always-on, harness-side)
2. Skill selector matches kind + keywords → loads ≤2 procedure bodies
   Priority: recovery cards > action cards > domain methodology
3. Model reads index, selects relevant procedure, executes step-by-step
4. Next turn: selector may swap bodies (old body evicted, new body loaded)
```

### 4.4 Chaining without blowing context

- **One procedure per situation, not one per task.** A rename task chains:
  `rename-via-write` (action) → `check-listed-files` (verify) → `verify-then-stop` (verify).
  Each is ≤500 w; only 2 loaded at once; third replaces first when situation shifts.
- **Procedure cards do NOT cross-reference each other** by body — only by name in the index.
  Cross-reference by name costs 1 line; cross-reference by body costs 500 w.
- **Domain methodology stays separate.** When a content CTA task hits,
  `content-cta-skeleton` (action) loads; `marketing-methodology` (domain) loads only
  if methodology question surfaces. They do not both load automatically.
- **Exit condition is always explicit.** Every card ends with a "Done when" clause
  that tells the model when to stop and not retry.

### 4.5 Anti-patterns (explicit rejects)

| anti-pattern | why rejected |
|---|---|
| Adding step-by-step recovery to `executor.md` | Grows always-on; every run pays the cost even when no recovery needed |
| Merging all Python situations into one fat `python-methodology` | Already ~230 w; adding rename+verify+two-module turns it into a 700 w blob that gets truncated |
| Procedure body references another procedure body inline | Doubles context load; use name-only pointers |
| Loading domain methodology skill for structure discipline | `content-cta-skeleton` card replaces this for structure; domain skill loads only for domain facts |
| Procedure card > 500 w | Hits SELECT_LIMIT body cap; excess gets truncated silently |

---

## 5. Card spec for ym0.14 authors

Each card in `skills/proc-<name>.md` must follow:

```toml
---
name = "proc-<name>"
kinds = ["<class>"]           # action | recovery | verify | context
description = "<trigger in ≤15 words>"
---
## <Name> (≤ 6 words)

Situation: <one sentence when this fires>

Steps:
1. …
2. …
(≤ 5 steps, each ≤ 2 lines)

## Done when
<one-sentence exit condition>
```

**Target: ≤ 350 w per card (leaves buffer under 500 w cap).**

---

## 6. What this design does NOT cover (out of scope)

- Authoring all 10 gap cards (ym0.14)
- Skill selector keyword tuning (ym0.14 implementation)
- `prompts/procedures.md` file creation (ym0.14)
- Task 4 think A/B (ym0.10, deferred P3)
- Prime Agent mechanisms (researcher brief pending — fold in when available)
- Fat manual refactor (existing domain methodology skills are not broken, just incomplete)
