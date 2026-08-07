---
description: Judge harness agent intelligence via Claude Sonnet CLI (-p)
argument-hint: [trace.jsonl | unit-dir | run-id]
allowed-tools: Bash(*), Read, Grep, Glob
---

<!--
Usage: /judge [path-to-trace.jsonl | benchmarks/.../unit | run-id]
Judge = Claude CLI Sonnet, effort medium. Subject = local harness agent (Qwopus).
Never use the subject model as judge.
-->

You are invoking the **external intelligence judge** for harness-core.

## Goal

Score the subject's *process* (mental model / trajectory), not only pass/fail.
Use the rubric at @eval/intelligence/RUBRIC.md.

## Resolve the subject artifact

Argument: `$ARGUMENTS`

1. If `$ARGUMENTS` is empty: ask for a `trace.jsonl`, a unit dir under `benchmarks/`, or a ledger `run_id` — then stop.
2. If it is a path to `trace.jsonl` (or ends with it): use that trace.
3. If it is a unit directory with `unit.toml`: prefer an existing recent trace for that unit; if none, run once:
   `OPENAI_BASE_URL=http://127.0.0.1:1234/v1 OPENAI_API_KEY=lm-studio uv run --extra deepagents harness run --unit $ARGUMENTS --backend deepagents --model openai:qwopus3.5-4b-coder-mtp`
   then locate the produced `trace.jsonl` (or reconstruct tool trajectory from run output / workspace).
4. If it looks like a 12-char hex run id: find matching log/workspace/trace under `data/logs/` if present.

Also collect: unit prompt (if any), verify exit if known, wall time if known.

## Call the judge (mandatory)

Build a prompt file under `/tmp/harness-judge-$$.md` that includes:

- The full rubric text from `eval/intelligence/RUBRIC.md`
- The task / unit prompt
- Verify exit / outcome summary
- The trace (truncate only if huge; keep all tool names + args summaries + final AI text)

Then run **exactly** this pattern (adjust model alias only if `sonnet` is unavailable):

```bash
claude -p "$(cat /tmp/harness-judge-$$.md)" \
  --model sonnet \
  --effort medium \
  --output-format json \
  --permission-mode dontAsk
```

Fail closed: if `claude` is missing or auth fails, report the error and **do not** fall back to a local LMS model as judge.

## Persist and report

1. Parse judge JSON (`scores`, `overall`, `rationale`). If the CLI wraps JSON, extract the object.
2. Write `eval/intelligence/runs/<utc>-<slug>.json` with: input path, model judged, judge model, scores, overall, rationale, timestamp.
3. Print a short table of the seven scores + overall + one-line rationale.

Do not commit. Do not touch `benchmarks/sealed/**` or genome-immutable paths.
