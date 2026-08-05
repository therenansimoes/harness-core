# ROADMAP

**Updated:** 2026-08-04. Live roadmap: `STATUS.md` says what exists and what is
broken; this file says what is next and why.

## The tune loop (concept adopted from skilltune.dev, studied 2026-08-04)

### How SkillTune works

Recorded here as studied, before any adaptation, so the mapping below can be
checked against the original instead of against a paraphrase.

- The unit of work is a **skill bundle**: a `SKILL.md` plus `references/`,
  `scripts/`, `evals/cases.jsonl` with an `expected-results.json`, and a
  `manifest.json` carrying a sha256 per directory. The bundle, not the prompt
  file alone, is what gets versioned.
- The lab **generates the eval cases** from a natural-language description of
  the skill plus a Q&A round on edge cases. The human approves **only the
  evals** — not the skill text. Once approved the cases are **immutable**: they
  are frozen before any tuning happens, so the target cannot drift toward
  whatever the tuner produced.
- The loop is **batch per artifact**: run the skill against the frozen cases,
  score, hand the scores and the current text to an LLM that **rewrites the
  whole `SKILL.md`**, emit a new version, re-run. It is not a diff proposal and
  not an A/B of variants — it is a versioned rewrite chain, v1..vN.
- The acceptance gate is **double**. (a) Strict monotonicity: a new version is
  kept only if it scores **above** its predecessor. (b) An absolute threshold
  (90/100) ends the loop. Every version is retained, so rollback is picking any
  earlier vN rather than reverting a patch.
- Scoring uses a **fixed multi-axis rubric** — structure, grounding, safety,
  clarity, coverage — on a 0-100 scale, averaged over roughly **4 trials per
  case**. Single-shot scores are not trusted; the average is the number the gate
  reads.
- Uplift is proved with a **triple baseline**: no-skill, first-draft skill,
  tuned skill. A real run reported 51 → 58 → 90+; the public case history shows
  v1 68.7 → v4 94.2.
- Each version carries a **short textual reason** for the change ("Tightened
  output contract"), producing an auditable semantic changelog. The whole
  history — versions, per-case scores, reasons — is exported as an
  `EVALUATION.md`.
- There is **no production telemetry** in the loop. The signal is 100% synthetic
  eval built before tuning. That is an assumed limitation, not an oversight.

### What generalizes — and how it maps here

| Concept | What the harness already has | What is missing |
| --- | --- | --- |
| Frozen human-approved evals | Unit corpus, frontier exams, and the human gates `seal`/`ack` that stay with the owner | An **eval-freeze primitive** scoped to one tunable artifact, separate from the sealed exam |
| Monotonic acceptance | The ruler/gate already judges a run against a frozen baseline and reverts on regression | An explicit per-artifact rule "a new version enters only if it scores above the previous one", with rollback defined as vN-1 |
| N trials against variance | `harness ab` already scores with Wilson intervals (`harness/ruler/wilson.py`) | Nothing new — reuse it as the trial aggregator instead of inventing an average |
| Multi-axis rubric | The ruler produces an aggregate score | Per-axis scoring (structure, grounding, safety, clarity, coverage) so a version can be rejected for the axis it broke |
| Triple baseline | A single frozen baseline per KPI | Adopt none/draft/tuned as the standard evidence triple for a tunable artifact |
| Textual reason per version | The `mutations` ledger already records verdicts and lineage | A short semantic reason string, made **mandatory** on every accepted version |
| sha256 integrity | The genome tamper fingerprint, frozen at `provision` and checked at `gate` | Extend the same fingerprint discipline to the artifact bundle, per directory |

The scope of the primitive is deliberately wider than SkillTune's. A **tunable
artifact** is any mutable zone of the genome: config TOMLs, prompt templates,
workflow definitions in `config/workflows/`, routing tables — not only skills. A
skill is simply the first case, because the dream action already produces skill
candidates and therefore already produces the input the loop needs.

### Where it runs

**During dreaming (primary).** `harness/improve/dream.py` today consolidates
episodic memory with no LLM: it fuses recurrent failures (same kind, same trace
signature, `MIN_RECURRENT` or more) into at most one skill candidate, and soft-
archives orphans older than `ORPHAN_AGE_DAYS`; both `propose_dream` and
`apply_dream` are fail-closed. The extension is that the candidate the sleep
proposes enters an **offline nightly tune loop** — local LM Studio/MLX, $0 —
against its frozen evals, and only the version that clears the monotonic gate is
handed to the normal attribution/lift/prune cycle. The sleep stops merely
proposing and starts proposing **already tuned**. The scoreboard still decides;
the sleep just arrives with better material.

**Online (secondary).** Real telemetry — the ledger, episodic memory — does
**not** enter the score. It enters two other places: the **generation of new
eval cases**, and the **election of which artifact joins the next dream's
queue**. This is the key insight taken from the study: a frozen eval used as an
offline gate is what keeps the tuner from reward-hacking its own signal.
Production feeds the case generator; it never touches the scoreboard.

### Phased plan

**D1 — eval-freeze primitive.** ✅ **Done 2026-08-04** (`harness/evals/`,
`harness eval freeze|verify|report`; first frozen bundle:
`evals/skills/python-fixes/`). A per-artifact eval bundle with an explicit
freeze step and a sha256 manifest, plus an `EVALUATION.md`-style export carrying
version history, per-case scores and reason strings. Deliverable: freeze an eval
set for one existing skill, then show that a later write to the case file is
rejected against the manifest.

**D2 — tune-loop action.** ✅ **Done 2026-08-05** (`harness/improve/tune.py`,
action `tune`, `harness tune`). A new action registered alongside the genome's 14,
operating on skills. Dream candidates go in; the winning version comes out with
its evidence attached (triple baseline, per-axis scores, reason string).
Deliverable: `uv run harness actions` lists it, and one dream candidate has a
recorded v1..vN chain where every retained version outscores its predecessor.

**D3 — generalize the tunable artifact.** ✅ **Done 2026-08-05** (`Tunable`
protocol in `harness/improve/tunable.py`; first workflow bundle:
`evals/config/workflows/hotfix/`). Lift the loop off skills and onto
config TOMLs, workflow definitions, prompt templates and routing tables.
Deliverable: the same action tunes a `config/workflows/*.toml` with no
skill-specific code path, and the fail-closed validation against `NODE_IMPLS`
still rejects a bad version before it is scored.

**D4 — online case mining.** ✅ **Done 2026-08-05** (`harness/evals/mining.py`,
`harness eval mine` / `eval seal-case --yes`). Ledger and episodic memory propose new eval cases;
the human seals them; sealed cases join the frozen suite for the next cycle.
Deliverable: a case that originated from a real recorded failure appears in a
frozen suite, with the human seal recorded, and the artifact's score moves
because of it.

**D5 — runtime topology governor.** ✅ **Done 2026-08-05, partial by design**
(`harness/governor/reorg.py` + hooks in `run_graph.py`; R1/R4 have material
effect, R2/R3 are recorded-only until a safe effect path exists — see the
mid-run insertion and fleet-collapse caveats in the reorg module).
(Adopted 2026-08-05 from the "dynamic
agent org" pattern: the org restructures itself mid-run; the extra structure
exists only while the work needs it.) Rule-driven reorganization fed by the
ledger, never by the eval scoreboard — frozen evals stay the only judge of
quality. Four rules: same failure kind twice → escalate the route to a
stronger model; failures concentrated in one area → insert a temporary
reviewer step that dissolves when quality settles; run cost exceeding the
task's value → collapse the fleet to one agent; trivial task → skip
orchestration entirely. Deliverable: a recorded run where a rule fires,
the topology change is written to the ledger with the triggering signal
attached, and the change reverts on its own once the signal clears.

## Known backlog (carried)

- `quickstart` / `doctor` polish — **resolved 2026-08-04** (fixed in this wave).
- Wheel packaging with `projects.toml` — **resolved 2026-08-04** (fixed in this
  wave).
- Fallback verify blind to newly created files — **resolved 2026-08-04** (fixed
  in this wave).
- `tests/test_dream.py::test_orfaos_arquivados_somem_do_recall` is a
  pre-existing red test, not a regression from this wave.
- LoRA fleet A/B/C runtime decision is still pending; the dossier and the three
  options are in `docs/RESEARCH-lora-fleet.md`.
