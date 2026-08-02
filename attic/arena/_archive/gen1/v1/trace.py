"""Trace + measurement: append-only JSONL log of every turn."""
import json
import time
from pathlib import Path


class Trace:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time()

    def log_turn(self, turn, action, result, tokens_in=0, tokens_out=0, cost_usd=0.0, ok=True):
        rec = {
            "turn": turn,
            "ts": round(time.time() - self.t0, 4),
            "action": action,
            "result": (result[:2000] if isinstance(result, str) else result),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "ok": ok,
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def summary(self):
        turns = []
        if self.path.exists():
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        turns.append(json.loads(line))
        return {
            "n_turns": len(turns),
            "total_tokens_in": sum(t.get("tokens_in", 0) for t in turns),
            "total_tokens_out": sum(t.get("tokens_out", 0) for t in turns),
            "total_cost_usd": sum(t.get("cost_usd", 0.0) for t in turns),
            "wall_time_s": turns[-1]["ts"] if turns else 0,
            "n_failed": sum(1 for t in turns if not t.get("ok", True)),
        }
