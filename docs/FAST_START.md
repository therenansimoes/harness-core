# Fast Start — from install to a real repo in one sitting

**Read `README.md` first** for what the harness is. This page is the shortest
path from a clone to the harness landing a reviewable branch on a repo you
actually own.

Everything below is the current CLI (`uv run harness <cmd> --help` is the source
of truth). Nothing here is a plan.

---

## 0. Install and prove the loop offline

```bash
uv sync --extra deepagents
uv run harness backends          # deterministic preflight, zero LLM calls
uv run harness run --unit tests/fixtures/echo --backend mock
```

The `mock` backend costs nothing and touches no network, so that third command
is the honest smoke test: if it does not print an `accept` line, stop here and
fix the install instead of pointing the harness at real code.

For a first run with an actual model, keep it free:

```bash
lms server start                 # LM Studio: OpenAI-compatible API on :1234
lms load qwen3.5-9b-mlx          # 18GB laptop: keep the local model <= 9B (MLX)
uv run harness run --unit tests/fixtures/tiny_fix \
  --backend deepagents --model openai:qwen3.5-9b-mlx
```

---

## 1. Register the project

```bash
uv run harness init /path/to/repo --name myapp \
  --build "npm run build" \
  --verify-default "npm test"
```

This writes an entry in `config/projects.toml`. `--build` runs in the worktree
before the unit's own `verify_cmd`; `--verify-default` is used by units that do
not declare one. The queue lives in `projects/<name>/queue` unless you pass
`--queue-dir`.

`init` only registers — it does not clone, and it does not touch the repo.

---

## 2. Author the work

```bash
uv run harness add --project myapp "add a health endpoint at /healthz" \
  --out-dir projects/myapp/queue
```

`add` reads the repo's real context (README, `package.json`, tree) and writes a
`unit.toml` plus `prompt.md`, with a `verify_cmd` it has to justify. Two flags
worth knowing:

- **`--dry`** prints the authored unit and writes nothing. Use it the first few
  times: a weak `verify_cmd` is the one failure mode the gate cannot rescue.
- **`--ui`** appends `harness ui-verify dist --expect-asset css` to the authored
  `verify_cmd` — for frontend work, where "tests pass" and "the page renders"
  are different claims.

Without `--out-dir` the unit lands in `benchmarks/quarantine/<slug>/`, which is
the staging area, **not** the project queue. Point `--out-dir` at the project's
queue when you want the unit to actually run.

Authoring calls a model (default `haiku`, capped by `--max-usd`, default 0.25).

---

## 3. Drain the queue

```bash
uv run harness queue --project myapp \
  --backend deepagents --model openai:qwen3.5-9b-mlx
```

Units run one at a time, in filename order, each in its own git worktree on an
ephemeral branch. The queue is **progressive**: filename order is dependency
order, so a unit that does not accept *stops* the loop rather than letting the
next one build on missing work.

Per unit:

| Outcome | What happens |
|---------|--------------|
| accept | delivery merged into the repo's default branch, branch `harness/<unit_id>` left for human review, unit moved to `queue/done/` |
| anything else | unit moved to `queue/stuck/`, loop stops |
| integration fails | treated exactly like stuck — the merge is part of the accept |

Useful flags: `--deadline-s` caps the whole loop (default 3600), `--attempts`
overrides the per-unit ceiling from `config/graph.toml`, and `--no-move` is a
dry run — it executes for real but leaves the queue untouched.

---

## 4. Check where you stand

```bash
uv run harness status
```

Counts pending / `done/` / `stuck/` per project plus total spend.

`scripts/queue_run.py` is a thin env-var wrapper over the same driver
(`harness/queue.py`) if you would rather drive it from cron.

---

## 5. When a unit gets stuck

The unit is in `queue/stuck/` and the ledger has the trace. In order:

1. Read the printed `decision.reason` — `verify_failed:exit=N` means the
   verifier ran and said no, which is usually the honest answer.
2. Re-read the authored `verify_cmd`. A verifier that cannot pass is a bad
   verifier, not a bad agent.
3. Move the unit back out of `stuck/` and re-run the queue after fixing either
   the prompt or the verifier.

Do **not** loosen the verifier to make the unit pass. That is the one change
that makes every later number meaningless.

---

## Where to go next

- `README.md` — what the harness is and what is actually proven.
- `STATUS.md` — live vs experimental surfaces, known gaps.
- `docs/ARCHITECTURE.md` — design detail.
- `CONTRIBUTING.md` — tests, backends, the genome rules.
- `docs/history/` — the original planning documents, kept for provenance.
