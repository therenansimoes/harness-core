"""Self-improvement loop: read own trace, propose a change to config.json,
gate it by re-running the task and comparing a deterministic metric, accept
or reject. No LLM in the gate — only code decides.
"""
import argparse
import json
import shutil
import tempfile
from pathlib import Path

import harness

CONFIG_PATH = Path(__file__).parent / "config.json"
EVOLVE_LOG = Path(__file__).parent / "evolve_log.jsonl"

DEFAULT_CONFIG = {"max_turns": 5}


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def metric_from_result(res, tr_summary):
    """Lower is better: turns to success, or +1000 penalty on failure."""
    if not res["success"]:
        return 10_000
    return res["turns"] + tr_summary.get("n_failed", 0) * 5


def propose_change(cfg, last_summary):
    """Deterministic proposal: if last run needed many turns, allow more;
    if it succeeded fast, try tightening the budget to save cost."""
    new_cfg = dict(cfg)
    if last_summary and last_summary.get("n_turns", 0) >= cfg["max_turns"]:
        new_cfg["max_turns"] = cfg["max_turns"] + 2
    elif last_summary and last_summary.get("n_turns", 99) <= max(1, cfg["max_turns"] - 2):
        new_cfg["max_turns"] = max(1, cfg["max_turns"] - 1)
    else:
        new_cfg["max_turns"] = cfg["max_turns"]
    return new_cfg


def run_and_measure(task_dir, cfg, work_dir):
    shutil.copytree(task_dir, work_dir, dirs_exist_ok=True)
    res = harness.run_task(work_dir, "Make the failing tests pass.", max_turns=cfg["max_turns"])
    from trace import Trace
    summary = Trace(Path(res["trace"])).summary()
    return res, summary


def evolve_once(task_dir):
    cfg = load_config()

    with tempfile.TemporaryDirectory() as baseline_dir:
        base_res, base_summary = run_and_measure(task_dir, cfg, Path(baseline_dir) / "t")
        base_metric = metric_from_result(base_res, base_summary)

    candidate_cfg = propose_change(cfg, base_summary)

    with tempfile.TemporaryDirectory() as cand_dir:
        cand_res, cand_summary = run_and_measure(task_dir, candidate_cfg, Path(cand_dir) / "t")
        cand_metric = metric_from_result(cand_res, cand_summary)

    accepted = cand_res["success"] and cand_metric <= base_metric
    if accepted:
        save_config(candidate_cfg)

    log_entry = {
        "base_config": cfg, "base_metric": base_metric,
        "candidate_config": candidate_cfg, "candidate_metric": cand_metric,
        "accepted": accepted,
    }
    with open(EVOLVE_LOG, "a") as f:
        f.write(json.dumps(log_entry) + "\n")
    return log_entry


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()
    for i in range(args.rounds):
        entry = evolve_once(args.task_dir)
        print(json.dumps(entry, indent=2))
