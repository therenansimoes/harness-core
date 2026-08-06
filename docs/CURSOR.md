# Cursor over `harness serve` + local bonsai (mlx)

## What it is

`harness serve` is an OpenAI-compatible gateway. For Cursor Agent, the model
to use is **`bonsai`**: a transparent proxy to `mlx_lm.server` that:

- accepts Cursor's mixed Chat Completions / Responses-shaped bodies
- remaps `reasoning` → `reasoning_content` so the thoughts panel works
- passes tool schemas through (Shell, TodoWrite, ApplyPatch, …) — Cursor
  owns the tool loop; this server does not reimplement it

Slash commands (`harness`, `harness:local`, …) still exist on the same port
but are a side path, not the Cursor Agent product.

## 1. Start mlx (no LM Studio)

```sh
# once: install mlx-lm outside this repo (uv.lock is genome-immutable)
pip install mlx-lm   # or: uv tool install mlx-lm

./scripts/mlx_bonsai.sh
# → mlx_lm.server on :1235 with Bonsai-27B-mlx-1bit
```

Weights default to `~/.lmstudio/models/prism-ml/Bonsai-27B-mlx-1bit` (already
on disk if you used LM Studio before). Override with `HARNESS_MLX_MODEL`.

## 2. Start the gateway

```sh
uv run harness serve --api-key "$HARNESS_SERVE_KEY"
```

Binds `127.0.0.1:8765` by default. Upstream chat uses `OPENAI_BASE_URL`
(default `http://127.0.0.1:1235/v1`).

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
| Model | `bonsai` |

Also set **Network → HTTP Compatibility Mode = HTTP/1.1** if streams fail.

Verify locally (before funnel):

```sh
curl -s http://127.0.0.1:8765/v1/models | jq '.data[].id'
# bonsai, harness, harness:local, …
```

## What Cursor gets

| Capability | Where it lives |
|------------|----------------|
| Tools / Shell / ApplyPatch | Cursor Agent (schemas proxied to the model) |
| Todo list | Cursor (`TodoWrite`) |
| Ask user | Cursor UI |
| Thinking panel | `delta.reasoning_content` remapped from mlx |
| File edits | Cursor tools — not harness `/do` |

## Safety

- Default bind is loopback. Non-loopback `--host` without a key refuses with 403.
- Funnel without `--api-key` silently serves the open internet — always set a key.
