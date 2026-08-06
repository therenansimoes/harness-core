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

`--port N`, `--host H` and `--api-key K` are available (key also via
`HARNESS_SERVE_KEY`, flag wins); a `--host` other than the loopback address
prints a warning to stderr (see "Safety scope").

## Point Cursor at it

Settings → Models → Add custom OpenAI-compatible provider:

- **API key**: whatever you pass to `harness serve --api-key <key>` (or
  export `HARNESS_SERVE_KEY=<key>`, flag wins). On loopback with no key
  configured the server accepts requests without one, same as before — the
  field just needs *some* value because most clients require non-empty. Off
  loopback a key is mandatory (see "Safety scope" below).
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
- `--max-usd` above `5.00` (or `<= 0`) is refused outright, before anything
  dispatches. What survives that gate travels in the argv of the spawned
  `harness do` and is a REAL hard cap on that run: `harness do --max-usd N`
  is checked fail-closed, before every dispatch (`governor/ceiling.py`), and
  stops the run — nothing applied — the moment cumulative spend across all
  attempts would reach `N`. `pressure.cost_cap_usd` (`config/governor.toml`,
  read via `governor.load_gov()`) is a separate, independent mechanism: an
  environment-wide cap, fail-open, checked after the money for an attempt is
  already spent. The `/do` reply prints both numbers side by side when the
  governor cap is set.
- Binds `127.0.0.1` by default. A non-loopback `--host` is accepted but
  prints a warning to stderr.
- With `--api-key`/`HARNESS_SERVE_KEY` set, every request needs
  `Authorization: Bearer <key>` (constant-time compare) or gets a 401 —
  applies to every route, streaming included. On loopback with no key
  configured, the server still accepts requests without one (unchanged
  default). Off loopback with no key, the server comes up refusing
  everything with a 403 and prints a help line to stderr instead of serving
  `/do` and the task queue to whoever can reach the port — same fail-closed
  rule `harness webhook` uses for a missing token.

## Tailscale

Cursor does not call your machine directly: its chat client's requests
originate from Cursor's own backend, so a tailnet-only route
(`http://<node>.<tailnet>.ts.net:8765`) is unreachable from there — Cursor
has no route into your tailnet. To point Cursor at `harness serve` running
on a box you're not sitting at, use `tailscale funnel` to publish it over
public HTTPS instead:

```sh
tailscale funnel 8765
```

That makes the port reachable from the open internet, so `--api-key` (or
`HARNESS_SERVE_KEY`) is mandatory here — **pass it yourself**. Funnel
forwards to your local port over loopback, so `harness serve`'s own
fail-closed check (which looks at the `--host` you bound to, not at who is
allowed to reach it) still sees `127.0.0.1` and won't turn auth on by
itself. Skipping `--api-key` with funnel running means the default
"loopback, no auth" behavior is silently serving `/do` and the task queue to
the open internet.

## Where it writes

Job records land under `$HARNESS_DATA_DIR/serve/jobs/<id>.json` (pid, task,
log path, start time, requested cap). The dispatched `harness do` run stamps
the ledger itself, same as running it from a terminal — `harness report`
picks it up like any other run.
