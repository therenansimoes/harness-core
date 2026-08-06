# Cursor over `harness serve`

## What it is

`harness serve` is a local, OpenAI-compatible chat endpoint over this repo's
harness. It is not an MCP server and it does not expose tools to a model —
it exposes itself as a fake "model" that a chat client can talk to. The
"model" is a closed router: a small set of `/slash` commands plus a
passthrough to a local LLM for free text.

## Start

```sh
uv run harness serve
```

Binds `127.0.0.1:8765` by default. The repo it acts on is the current
working directory at startup, pinned once — a later request never changes
which repo `/do` runs in, even if the client sends a different one.

`--port N` and `--host H` are available; a `--host` other than the loopback
address prints a warning to stderr (see "Safety scope").

## Point Cursor at it

Settings → Models → Add custom OpenAI-compatible provider:

- **API key**: any non-empty string (ignored by the server).
- **Base URL**: `http://127.0.0.1:8765/v1`
- **Model**: `harness`

Verify with:

```sh
curl -s http://127.0.0.1:8765/v1/models
```

## Slash commands

```
/status                      — doctor, self-approval state, jobs in flight
/ready                       — ready tasks (bd ready)
/queue                       — proposals waiting on a human (selfapprove queue)
/history                     — decisions already made (selfapprove history)
/market <term>                — search the skill marketplace (read-only)
/new <title>                 — create a task (bd create)
/close <id>                  — close a task (bd close)
/do <request> [--max-usd N]  — run `harness do` in the background (cap 5.00)
/help                        — this list
```

## Free text

Text without a leading `/` goes to the local LLM at `OPENAI_BASE_URL`
(default `http://127.0.0.1:1234/v1`, i.e. LM Studio). If that endpoint is
unreachable or returns garbage, the reply falls back to the deterministic
command list instead of a raw error.

## Safety scope

The router is a **closed dict** of command handlers — there is no code path
from this module to `market approve`, `selfapprove approve/undo`, `seal`, or
any write to `config/genome.toml`. Nothing here can grant itself more
permission than a read.

- `/do` always runs `harness do <task> --no-apply`: the result lands on a
  delivery branch, nothing merges into the default branch on its own.
- Only one `/do` job runs at a time (`MAX_RUNNING_JOBS = 1`); a second
  request while one is in flight is refused.
- `--max-usd` above `5.00` is refused outright. That refusal is enforcement
  of the request, not of the run: the actual ceiling a dispatched `harness
  do` obeys is `pressure.cost_cap_usd` in `config/governor.toml` (read via
  `governor.load_gov()`), and the `/do` reply prints that real number next
  to the one the user asked for. If the governor's cap is `0` (unset), the
  reply says so explicitly instead of pretending `--max-usd` was honored.
- Binds `127.0.0.1` by default. A non-loopback `--host` is accepted but
  prints a warning to stderr — this server has no auth of its own, so
  exposing it to a network hands out `/do` and the task queue to anyone who
  can reach the port.

## Where it writes

Job records land under `$HARNESS_DATA_DIR/serve/jobs/<id>.json` (pid, task,
log path, start time, requested cap). The dispatched `harness do` run stamps
the ledger itself, same as running it from a terminal — `harness report`
picks it up like any other run.
