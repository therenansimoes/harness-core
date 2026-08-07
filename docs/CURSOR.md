# Cursor over `harness serve` + local Qwopus (LM Studio)

## What it is

`harness serve` is an OpenAI-compatible gateway. For Cursor Agent, the model
to use is **`qwopus3.5-4b-coder-mtp`**: a transparent proxy to LM Studio that:

- accepts Cursor's mixed Chat Completions / Responses-shaped bodies
- remaps `reasoning` → `reasoning_content` so the thoughts panel works
- passes tool schemas through (Shell, TodoWrite, ApplyPatch, …) — Cursor
  owns the tool loop; this server does not reimplement it
- keeps **thinking ON** by default (`chat_template_kwargs.enable_thinking=true`)

Slash commands (`harness`, `harness:local`, …) still exist on the same port
but are a side path, not the Cursor Agent product.

## 1. Start LM Studio (offload)

```sh
lms server start
lms unload --all
lms load qwopus3.5-4b-coder-mtp -y -c 8192 --gpu max --parallel 1 \
  --speculative-draft-mtp --identifier qwopus3.5-4b-coder-mtp
```

Upstream is `http://127.0.0.1:1234/v1`. The gateway also:
- trims prompts (`HARNESS_SERVE_MAX_PROMPT_CHARS`, default 24000 chars)
- clamps `max_tokens` (`HARNESS_SERVE_MAX_TOKENS`, default 4096)
- enables thinking unless `HARNESS_SERVE_DISABLE_THINKING=1`
- logs free/available memory and 503s under critical pressure

A/B latency (thinking ON, basic message):

```sh
python3 scripts/bench_lms_models.py --models bonsai-1bit,qwopus3.5-4b-coder-mtp
# → median wall/TTFT/reasoning_tokens; JSON in /tmp/harness-model-bench.json
```

## 2. Start the gateway

```sh
export HARNESS_UPSTREAM_MODEL=qwopus3.5-4b-coder-mtp
uv run harness serve --api-key "$HARNESS_SERVE_KEY"
```

Binds `127.0.0.1:8765` by default. Upstream chat uses `OPENAI_BASE_URL`
(default `http://127.0.0.1:1234/v1`).

## 3. Expose HTTPS (Cursor cannot hit localhost)

Cursor BYOK requests leave Cursor's cloud, so `127.0.0.1` is SSRF-blocked.
Publish the port:

```sh
tailscale funnel 8765
# or: cloudflared tunnel --url http://127.0.0.1:8765
```

Pass `--api-key` (or `HARNESS_SERVE_KEY`) yourself — funnel forwards over
loopback, so the server's own off-loopback check will not force auth on.

## 4. Point Cursor at it

Settings → Models → OpenAI-compatible / Override Base URL:

| Field | Value |
|--------|--------|
| API key | same as `--api-key` / `HARNESS_SERVE_KEY` |
| Base URL | `https://<your-funnel-host>/v1` |
| Model | `qwopus3.5-4b-coder-mtp` |

(`bonsai` / `qwopus` still route to the same proxy for old configs.)

Also set **Network → HTTP Compatibility Mode = HTTP/1.1** if streams fail.

Verify locally (before funnel):

```sh
curl -s http://127.0.0.1:8765/v1/models | jq '.data[].id'
# qwopus3.5-4b-coder-mtp, harness, harness:local, …
```

## What Cursor gets

| Capability | Where it lives |
|------------|----------------|
| Tools / Shell / ApplyPatch | Cursor Agent (schemas proxied to the model) |
| Todo list | Cursor (`TodoWrite`) |
| Ask user | Cursor UI |
| Thinking panel | `delta.reasoning_content` remapped from LMS |
| File edits | Cursor tools — not harness `/do` |

## Safety

- Default bind is loopback. Non-loopback `--host` without a key refuses with 403.
- Genome zones still apply to harness mutations; this gateway does not bypass them.
