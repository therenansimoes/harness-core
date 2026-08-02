"""Per-turn trace: what happened, cost in USD, tokens, wall time.

Named tracelib (not trace.py) to avoid shadowing the stdlib `trace` module —
a bug inherited from the parent generation.
"""
import json
import time
from pathlib import Path

# USD per token, input/output — anthropic claude-sonnet-4-5 public pricing
# (per-million: $3 in / $15 out) kept as a constant so cost is auditable.
PRICE_PER_TOKEN = {
    "claude-sonnet-4-5": {"in": 3.0 / 1_000_000, "out": 15.0 / 1_000_000},
}


def price_usd(model, tokens_in, tokens_out):
    p = PRICE_PER_TOKEN.get(model, {"in": 0.0, "out": 0.0})
    return tokens_in * p["in"] + tokens_out * p["out"]


class Trace:
    def __init__(self, path: Path):
        self.path = Path(path)

    def log_turn(self, turn, action, output, ok, tokens_in=0, tokens_out=0, cost_usd=0.0, t0=None):
        entry = {
            "turn": turn,
            "action": action,
            "output_tail": (output or "")[-800:],
            "ok": ok,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost_usd, 8),
            "wall_s": round(time.time() - t0, 4) if t0 else 0.0,
            "ts": time.time(),
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def entries(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def summary(self):
        entries = self.entries()
        n_turns = len({e["turn"] for e in entries})
        n_failed = sum(1 for e in entries if not e["ok"])
        cost_usd = sum(e.get("cost_usd", 0.0) for e in entries)
        tokens_in = sum(e.get("tokens_in", 0) for e in entries)
        tokens_out = sum(e.get("tokens_out", 0) for e in entries)
        wall_s = sum(e.get("wall_s", 0.0) for e in entries)
        return {
            "n_turns": n_turns,
            "n_failed": n_failed,
            "cost_usd": round(cost_usd, 8),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "wall_s": round(wall_s, 4),
        }
