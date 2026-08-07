#!/usr/bin/env python3
"""A/B latency: LM Studio models with thinking ON + a basic message.

Measures TTFT (first SSE token), wall time, completion/reasoning tokens.
Does not go through harness serve — hits OPENAI_BASE_URL / :1234 directly.

Example:
  python3 scripts/bench_lms_models.py
  python3 scripts/bench_lms_models.py --models bonsai,qwopus3.5-4b-coder-mtp --reps 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:1234/v1"
DEFAULT_MODELS = ("bonsai", "qwopus3.5-4b-coder-mtp")
BASIC_PROMPT = "diga oi em uma palavra"
DEFAULT_OUT = "/tmp/harness-model-bench.json"

# Map API id → (lms model-key, preferred load identifier)
# bonsai-1bit needs the LM Studio Python SDK — `lms load …@1bit` fails even when
# the weights are on disk; SDK resolves `prism-ml/bonsai-27b@1bit`.
LOAD_KEYS: dict[str, tuple[str, str]] = {
    "bonsai": ("prism-ml/bonsai-27b", "bonsai"),
    "bonsai-1bit": ("prism-ml/bonsai-27b@1bit", "prism-ml/bonsai-27b@1bit"),
    "bonsai-2bit": ("prism-ml/bonsai-27b@2bit", "bonsai"),
    "qwopus3.5-4b-coder-mtp": ("qwopus3.5-4b-coder-mtp", "qwopus3.5-4b-coder-mtp"),
}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(statistics.median(xs))


def _run(cmd: list[str], *, timeout_s: float = 300.0) -> None:
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, check=True, timeout=timeout_s)


def unload_all() -> None:
    # Best-effort: unload known ids then --all
    for ident in (
        "bonsai",
        "bonsai-1bit",
        "prism-ml/bonsai-27b@1bit",
        "prism-ml/bonsai-27b@prism-ml/Bonsai-27B-mlx-1bit",
        "qwopus3.5-4b-coder-mtp",
    ):
        try:
            _run(["lms", "unload", ident], timeout_s=120.0)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    try:
        _run(["lms", "unload", "--all"], timeout_s=120.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass


def _set_bonsai_thinking_default(enabled: bool) -> None:
    """LMS MLX ignores request kwargs for bonsai — hub model.yaml default wins."""
    path = (
        Path.home()
        / ".lmstudio/hub/models/prism-ml/bonsai-27b/model.yaml"
    )
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    marker = "  - key: enableThinking\n"
    i = text.find(marker)
    if i < 0:
        return
    j = text.find("defaultValue:", i)
    if j < 0:
        return
    k = text.find("\n", j)
    line = text[j:k]
    want = f"defaultValue: {'true' if enabled else 'false'}"
    if want in line:
        return
    path.write_text(text[:j] + want + text[k:], encoding="utf-8")
    print(f"[bench] bonsai enableThinking default → {enabled}", file=sys.stderr)


def _load_via_sdk(key: str, *, context: int) -> str:
    """Load model through lmstudio Python SDK (needed for @1bit variants)."""
    code = (
        "import lmstudio as lms\n"
        "from lmstudio import LlmLoadModelConfig\n"
        f"key = {key!r}\n"
        "for m in list(lms.list_loaded_models()):\n"
        "    try:\n"
        "        m.unload()\n"
        "    except Exception:\n"
        "        pass\n"
        f"cfg = LlmLoadModelConfig(context_length={int(context)})\n"
        "h = lms.llm(key, config=cfg, ttl=None)\n"
        "print(h.identifier)\n"
    )
    print(f"+ uv run --with lmstudio (load {key})", file=sys.stderr)
    out = subprocess.check_output(
        ["uv", "run", "--with", "lmstudio>=1.0", "python", "-c", code],
        timeout=600.0,
        text=True,
    )
    ident = out.strip().splitlines()[-1].strip()
    if not ident:
        raise RuntimeError(f"SDK load returned empty identifier for {key}")
    return ident


def load_model(api_id: str, *, context: int, parallel: int, mtp: bool, think: bool) -> str:
    key, ident = LOAD_KEYS.get(api_id, (api_id, api_id))
    if api_id.startswith("bonsai") or "bonsai" in key:
        _set_bonsai_thinking_default(think)
    # Variant pins (@1bit / @2bit): CLI often cannot resolve — SDK first.
    if "@" in key or api_id.endswith("-1bit"):
        return _load_via_sdk(key, context=context)
    cmd = [
        "lms",
        "load",
        key,
        "-y",
        "-c",
        str(context),
        "--gpu",
        "max",
        "--parallel",
        str(parallel),
        "--identifier",
        ident,
    ]
    if mtp:
        cmd.append("--speculative-draft-mtp")
    try:
        _run(cmd, timeout_s=600.0)
        return ident
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(f"[bench] lms load failed for {key}; trying SDK", file=sys.stderr)
        return _load_via_sdk(key, context=context)


def chat_stream(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    think: bool,
    timeout_s: float,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": think},
    }
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    t0 = time.perf_counter()
    ttft: float | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    finish: str | None = None

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch0 = choices[0] if isinstance(choices[0], dict) else {}
                delta = ch0.get("delta") or {}
                if not isinstance(delta, dict):
                    delta = {}
                piece = delta.get("content") or ""
                reason = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if (piece or reason) and ttft is None:
                    ttft = time.perf_counter() - t0
                if piece:
                    content_parts.append(str(piece))
                if reason:
                    reasoning_parts.append(str(reason))
                fr = ch0.get("finish_reason")
                if fr:
                    finish = str(fr)
    except urllib.error.HTTPError as exc:
        err = exc.read()[:500].decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {err}") from exc

    wall = time.perf_counter() - t0
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    completion = int(usage.get("completion_tokens") or 0)
    reasoning_toks = 0
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning_toks = int(details.get("reasoning_tokens") or 0)
    if reasoning_toks == 0 and reasoning:
        # fallback estimate when usage omits reasoning split
        reasoning_toks = max(0, len(reasoning.split()))

    decode_s = wall - (ttft or 0.0)
    tok_s = (completion / decode_s) if decode_s > 0.05 and completion else None

    return {
        "wall_s": round(wall, 3),
        "ttft_s": None if ttft is None else round(ttft, 3),
        "tok_per_s": None if tok_s is None else round(tok_s, 2),
        "completion_tokens": completion,
        "reasoning_tokens": reasoning_toks,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "finish_reason": finish,
        "hit_max_tokens": finish == "length",
        "content": content[:200],
        "reasoning_preview": reasoning[:200],
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    walls = [float(r["wall_s"]) for r in runs]
    ttfts = [float(r["ttft_s"]) for r in runs if r.get("ttft_s") is not None]
    comps = [int(r["completion_tokens"]) for r in runs]
    reasons = [int(r["reasoning_tokens"]) for r in runs]
    return {
        "n": len(runs),
        "wall_s_median": _median(walls),
        "ttft_s_median": _median(ttfts),
        "completion_tokens_median": _median([float(x) for x in comps]),
        "reasoning_tokens_median": _median([float(x) for x in reasons]),
        "any_hit_max_tokens": any(bool(r.get("hit_max_tokens")) for r in runs),
        "runs": runs,
    }


def bench_one(
    *,
    api_id: str,
    base_url: str,
    prompt: str,
    reps: int,
    max_tokens: int,
    think: bool,
    context: int,
    parallel: int,
    mtp: bool,
    timeout_s: float,
) -> dict[str, Any]:
    unload_all()
    served = load_model(api_id, context=context, parallel=parallel, mtp=mtp, think=think)
    print(f"[bench] warmup {served} think={think}", file=sys.stderr)
    try:
        chat_stream(
            base_url=base_url,
            model=served,
            prompt=prompt,
            max_tokens=max_tokens,
            think=think,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # warmup best-effort
        print(f"[bench] warmup failed: {exc}", file=sys.stderr)

    runs: list[dict[str, Any]] = []
    for i in range(reps):
        print(f"[bench] {served} rep {i + 1}/{reps}", file=sys.stderr)
        runs.append(
            chat_stream(
                base_url=base_url,
                model=served,
                prompt=prompt,
                max_tokens=max_tokens,
                think=think,
                timeout_s=timeout_s,
            )
        )
    return {"model": api_id, "served_as": served, "think": think, "prompt": prompt, **summarize(runs)}


def print_table(results: list[dict[str, Any]]) -> None:
    cols = (
        "model",
        "wall_s_median",
        "ttft_s_median",
        "completion_tokens_median",
        "reasoning_tokens_median",
        "hit_max",
    )
    print("| " + " | ".join(cols) + " |")
    print("| " + " | ".join("---" for _ in cols) + " |")
    for r in results:
        row = [
            str(r.get("model")),
            f"{r.get('wall_s_median'):.3f}" if r.get("wall_s_median") is not None else "—",
            f"{r.get('ttft_s_median'):.3f}" if r.get("ttft_s_median") is not None else "—",
            f"{r.get('completion_tokens_median'):.0f}"
            if r.get("completion_tokens_median") is not None
            else "—",
            f"{r.get('reasoning_tokens_median'):.0f}"
            if r.get("reasoning_tokens_median") is not None
            else "—",
            "yes" if r.get("any_hit_max_tokens") else "no",
        ]
        print("| " + " | ".join(row) + " |")


def restore_bonsai(*, context: int, parallel: int, mtp: bool) -> None:
    unload_all()
    # Cursor gateway wants thinking off + 2bit (speed). Prefer CLI identifier bonsai.
    try:
        _set_bonsai_thinking_default(False)
        _run(
            [
                "lms",
                "load",
                "prism-ml/bonsai-27b",
                "-y",
                "-c",
                str(context),
                "--gpu",
                "max",
                "--parallel",
                str(parallel),
                "--identifier",
                "bonsai",
                "--speculative-draft-mtp",
            ],
            timeout_s=600.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        load_model("bonsai-2bit", context=context, parallel=parallel, mtp=mtp, think=False)
    print("[bench] restored bonsai (2bit, think off) for Cursor gateway", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="comma-separated API ids (default: bonsai,qwopus3.5-4b-coder-mtp)",
    )
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--prompt", default=BASIC_PROMPT)
    ap.add_argument("--context", type=int, default=8192)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--no-mtp", action="store_true")
    ap.add_argument("--think", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--no-restore-bonsai", action="store_true")
    args = ap.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("no models", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    mtp = not args.no_mtp
    try:
        for mid in models:
            results.append(
                bench_one(
                    api_id=mid,
                    base_url=args.base_url,
                    prompt=args.prompt,
                    reps=max(1, args.reps),
                    max_tokens=args.max_tokens,
                    think=bool(args.think),
                    context=args.context,
                    parallel=args.parallel,
                    mtp=mtp,
                    timeout_s=args.timeout,
                )
            )
    finally:
        if not args.no_restore_bonsai:
            try:
                restore_bonsai(context=args.context, parallel=args.parallel, mtp=mtp)
            except Exception as exc:
                print(f"[bench] restore bonsai failed: {exc}", file=sys.stderr)

    payload = {
        "prompt": args.prompt,
        "think": bool(args.think),
        "max_tokens": args.max_tokens,
        "reps": args.reps,
        "base_url": args.base_url,
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print_table(results)
    print(f"\nWrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
