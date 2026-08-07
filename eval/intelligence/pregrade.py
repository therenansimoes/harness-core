"""Process KPI extractor — offline, no LLM calls.

Usage:
    uv run python eval/intelligence/pregrade.py [--run-id RUN_ID] [--unit UNIT_ID]
    uv run python eval/intelligence/pregrade.py --all --limit 50

Reads trace.*.jsonl from data/logs/<run_id>/ (same shape as
_write_trace / _tools_in_trace in deepagents_backend + procedural).
Joins ledger ok/sec_total/exit_reason when available.
Reports per-run:
  - empty_turn_rate     fraction of AI turns with no content and no tool_calls
  - first_tool_turn     turn index (1-based) of first tool_call; None if never
  - tool_calls_per_turn mean tool_calls per AI turn
  - listed_path_cov     fraction of UnitSpec.files found in files_changed
                        (None when files list is empty or files_changed unknown)
Failure class tags (context|action|recovery|verify|salvage) from exit_reason.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Trace parsing (mirrors procedural._tools_in_trace / _write_trace)
# ---------------------------------------------------------------------------

def _load_trace_lines(run_dir: Path) -> list[dict[str, Any]]:
    """Return all valid JSONL objects from trace.*.jsonl, ordered by filename."""
    objs: list[dict[str, Any]] = []
    for p in sorted(run_dir.glob("trace.*.jsonl")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                objs.append(obj)
    return objs


def _is_ai(obj: dict[str, Any]) -> bool:
    return obj.get("type") in ("ai", "assistant")


def _tool_calls(obj: dict[str, Any]) -> list[str]:
    return obj.get("tool_calls") or []


def compute_kpis(run_dir: Path) -> dict[str, Any]:
    """Compute process KPIs for a single run directory."""
    objs = _load_trace_lines(run_dir)
    ai_turns = [o for o in objs if _is_ai(o)]

    empty_turns = 0
    total_tool_calls = 0
    first_tool_turn: int | None = None

    for i, obj in enumerate(ai_turns, start=1):
        calls = _tool_calls(obj)
        content = (obj.get("content") or "").strip()
        if not content and not calls:
            empty_turns += 1
        total_tool_calls += len(calls)
        if calls and first_tool_turn is None:
            first_tool_turn = i

    n = len(ai_turns)
    return {
        "n_ai_turns": n,
        "empty_turn_rate": round(empty_turns / n, 3) if n else None,
        "first_tool_turn": first_tool_turn,
        "tool_calls_per_turn": round(total_tool_calls / n, 2) if n else None,
        "empty_turns": empty_turns,
        "total_tool_calls": total_tool_calls,
    }


# ---------------------------------------------------------------------------
# Ledger join
# ---------------------------------------------------------------------------

def _ledger_row(run_id: str) -> dict[str, Any] | None:
    try:
        from harness.ledger import store
        rows = store.history(limit=2000)
        for r in rows:
            if r.run_id == run_id:
                return {
                    "ok": r.ok,
                    "exit_reason": r.exit_reason,
                    "sec_total": r.sec_total,
                    "unit_id": r.unit_id,
                    "model": r.model,
                    "files_changed": None,  # not stored in ledger
                }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Failure class tag
# ---------------------------------------------------------------------------

_TAG_MAP = {
    "max_turns": "action",
    "loop": "action",
    "stalled": "action",
    "timeout": "context",
    "context": "context",
    "tool_error": "salvage",
    "verify": "verify",
    "blocker": "recovery",
    "cancelled": "recovery",
}


def failure_tag(exit_reason: str | None) -> str:
    if not exit_reason:
        return "unknown"
    er = exit_reason.lower()
    for key, tag in _TAG_MAP.items():
        if key in er:
            return tag
    return "action"


# ---------------------------------------------------------------------------
# Listed-path coverage
# ---------------------------------------------------------------------------

def listed_path_coverage(
    expected_files: list[str],
    files_changed: list[str] | None,
) -> float | None:
    """Fraction of expected files present in files_changed. None if unknown."""
    if not expected_files:
        return None
    if files_changed is None:
        return None
    hit = sum(1 for f in expected_files if f in files_changed)
    return round(hit / len(expected_files), 3)


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    run_id: str
    unit_id: str | None
    ok: bool | None
    exit_reason: str | None
    failure_class: str
    sec_total: float | None
    n_ai_turns: int
    empty_turn_rate: float | None
    first_tool_turn: int | None
    tool_calls_per_turn: float | None
    listed_path_cov: float | None


def grade_run(run_id: str, logs_root: Path) -> RunReport | None:
    run_dir = logs_root / run_id
    if not run_dir.is_dir():
        return None
    kpis = compute_kpis(run_dir)
    ledger = _ledger_row(run_id)
    unit_id = ledger["unit_id"] if ledger else None
    ok = ledger["ok"] if ledger else None
    exit_reason = ledger["exit_reason"] if ledger else None
    sec_total = ledger["sec_total"] if ledger else None

    # listed-path coverage: load UnitSpec.files if we can resolve unit
    listed_cov: float | None = None
    if unit_id:
        try:
            from harness.cli import _load_unit  # type: ignore[attr-defined]
            spec = _load_unit(unit_id)
            if spec and spec.files:
                listed_cov = listed_path_coverage(list(spec.files), None)
        except Exception:
            pass

    return RunReport(
        run_id=run_id,
        unit_id=unit_id,
        ok=ok,
        exit_reason=exit_reason,
        failure_class=failure_tag(exit_reason),
        sec_total=sec_total,
        n_ai_turns=kpis["n_ai_turns"],
        empty_turn_rate=kpis["empty_turn_rate"],
        first_tool_turn=kpis["first_tool_turn"],
        tool_calls_per_turn=kpis["tool_calls_per_turn"],
        listed_path_cov=listed_cov,
    )


def _fmt(v: Any) -> str:
    return "—" if v is None else str(v)


def print_report(reports: list[RunReport], verbose: bool = False) -> None:
    if not reports:
        print("No runs found.")
        return
    header = (
        f"{'run_id':>14}  {'unit':>30}  {'ok':>4}  "
        f"{'etr':>5}  {'ftt':>4}  {'tpt':>5}  {'sec':>6}  {'class':>10}  {'cov':>5}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for r in reports:
        print(
            f"{r.run_id[:14]:>14}  {(_fmt(r.unit_id))[:30]:>30}  "
            f"{_fmt(r.ok):>4}  "
            f"{_fmt(r.empty_turn_rate):>5}  "
            f"{_fmt(r.first_tool_turn):>4}  "
            f"{_fmt(r.tool_calls_per_turn):>5}  "
            f"{_fmt(r.sec_total):>6}  "
            f"{r.failure_class:>10}  "
            f"{_fmt(r.listed_path_cov):>5}"
        )
    print(sep)
    # Aggregate
    n = len(reports)
    n_ok = sum(1 for r in reports if r.ok)
    etrs = [r.empty_turn_rate for r in reports if r.empty_turn_rate is not None]
    ftts = [r.first_tool_turn for r in reports if r.first_tool_turn is not None]
    tpts = [r.tool_calls_per_turn for r in reports if r.tool_calls_per_turn is not None]

    def avg(xs: list) -> str:
        return f"{sum(xs)/len(xs):.3f}" if xs else "—"

    print(
        f"\nN={n}  ok={n_ok}/{n}  "
        f"avg_etr={avg(etrs)}  avg_ftt={avg(ftts)}  avg_tpt={avg(tpts)}"
    )
    from collections import Counter
    tags = Counter(r.failure_class for r in reports if not r.ok)
    if tags:
        print("Failure classes:", dict(tags))


def _logs_root() -> Path:
    try:
        from harness.ledger import store
        from harness.ruler.verify import LOGS_REL
        return store.data_dir() / LOGS_REL
    except Exception:
        return Path("data/logs")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process KPI grader (offline).")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--run-id", help="Single run_id to grade.")
    g.add_argument("--all", action="store_true", help="Grade all run_ids in data/logs.")
    parser.add_argument("--unit", help="Filter by unit_id (--all only).")
    parser.add_argument("--limit", type=int, default=50, help="Max runs to show (--all).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logs_root = _logs_root()
    reports: list[RunReport] = []

    if args.run_id:
        r = grade_run(args.run_id, logs_root)
        if r:
            reports = [r]
        else:
            print(f"Run not found: {args.run_id}")
    else:
        if not logs_root.is_dir():
            print(f"No logs dir at {logs_root}")
            return
        run_ids = [
            p.name
            for p in sorted(
                (p for p in logs_root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        ][: args.limit]
        for rid in run_ids:
            r = grade_run(rid, logs_root)
            if r is None:
                continue
            if args.unit and r.unit_id != args.unit:
                continue
            reports.append(r)

    print_report(reports, verbose=args.verbose)


if __name__ == "__main__":
    main()
