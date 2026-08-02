"""Self-improvement loop: read own trace, propose a change to config.json,
gate it by re-running the task TWICE in disposable copies (baseline config
vs candidate config), compare a deterministic metric computed from the
trace, accept or reject. No LLM in the gate — only code decides.

Metric rewards fewer turns AND a lower max_turns cap (proxy for worst-case
cost), so a config that still succeeds with a tighter budget is a genuine
improvement. Acceptance requires a STRICT improvement (candidate_metric <
base_metric) and candidate success — equal-or-worse is rejected and rolled
back, which is what happens once the budget is driven down to the point
where the task can no longer finish in time: a real, non-gamed rejection.
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import harness
from tracelib import Trace

CONFIG_PATH = Path(__file__).parent / "config.json"
EVOLVE_LOG = Path(__file__).parent / "evolve_log.jsonl"

DEFAULT_CONFIG = {"max_turns": 6}


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def metric_from_result(res, summary, cfg):
    """Lower is better. Hard failure is penalized far above any real run.
    Successes are scored on turns actually used plus a small tie-breaker for
    a lower max_turns cap, so tightening the budget without extra turns is a
    genuine (if marginal) improvement, while a candidate that fails is
    always strictly worse than any candidate that succeeds."""
    if not res["success"]:
        return 100_000 + summary.get("n_failed", 0)
    return res["turns"] * 10 + summary.get("n_failed", 0) * 5 + cfg["max_turns"] * 0.1


def propose_change(cfg):
    """Deterministic, monotonic cost-cutting exploration: always try one
    less turn of budget than currently configured. No fixed point trap:
    once max_turns==1 the proposal is identical to cfg, and identical
    proposals cannot pass the strict-improvement gate below — they are
    rejected explicitly rather than silently re-accepted."""
    return {"max_turns": max(1, cfg["max_turns"] - 1)}


def run_and_measure(task_dir, cfg, work_dir, instruction, test_cmd=None):
    shutil.copytree(task_dir, work_dir, dirs_exist_ok=True)
    res = harness.run_task(work_dir, instruction, max_turns=cfg["max_turns"], test_cmd=test_cmd)
    summary = Trace(Path(res["trace"])).summary()
    return res, summary


def evolve_once(task_dir, instruction="Make the failing tests pass.", test_cmd=None):
    cfg = load_config()
    candidate_cfg = propose_change(cfg)

    if candidate_cfg == cfg:
        entry = {
            "base_config": cfg, "candidate_config": candidate_cfg,
            "accepted": False, "reason": "no-op proposal (budget floor reached)",
        }
        with open(EVOLVE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    with tempfile.TemporaryDirectory() as baseline_dir:
        base_res, base_summary = run_and_measure(task_dir, cfg, Path(baseline_dir) / "t", instruction, test_cmd)
        base_metric = metric_from_result(base_res, base_summary, cfg)

    with tempfile.TemporaryDirectory() as cand_dir:
        cand_res, cand_summary = run_and_measure(task_dir, candidate_cfg, Path(cand_dir) / "t", instruction, test_cmd)
        cand_metric = metric_from_result(cand_res, cand_summary, candidate_cfg)

    accepted = cand_res["success"] and cand_metric < base_metric
    if accepted:
        save_config(candidate_cfg)

    entry = {
        "base_config": cfg, "base_metric": base_metric, "base_success": base_res["success"],
        "base_summary": base_summary,
        "candidate_config": candidate_cfg, "candidate_metric": cand_metric, "candidate_success": cand_res["success"],
        "candidate_summary": cand_summary,
        "accepted": accepted,
        "reason": "strict improvement" if accepted else "candidate not strictly better (rolled back)",
    }
    with open(EVOLVE_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--instruction", default="Make the failing tests pass.")
    ap.add_argument("--test-cmd", default=None, help="override test command, e.g. 'pytest -q -k foo'")
    args = ap.parse_args()
    test_cmd = args.test_cmd.split() if args.test_cmd else None
    for i in range(args.rounds):
        entry = evolve_once(args.task_dir, args.instruction, test_cmd)
        print(json.dumps(entry, indent=2))
