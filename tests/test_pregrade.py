"""Tests for eval/intelligence/pregrade.py — offline KPI extractor."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# pregrade lives in eval/intelligence, not a package; load via spec
_PREGRADE = Path(__file__).parent.parent / "eval" / "intelligence" / "pregrade.py"
_spec = importlib.util.spec_from_file_location("pregrade", _PREGRADE)
pregrade = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["pregrade"] = pregrade  # register so @dataclass can resolve __module__
_spec.loader.exec_module(pregrade)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_trace(tmp_path: Path, lines: list[dict]) -> Path:
    run_dir = tmp_path / "run_abc"
    run_dir.mkdir(parents=True)
    p = run_dir / "trace.0.jsonl"
    p.write_text("\n".join(json.dumps(obj) for obj in lines) + "\n")
    return run_dir


# ---------------------------------------------------------------------------
# _load_trace_lines
# ---------------------------------------------------------------------------


def test_load_trace_lines_basic(tmp_path):
    run_dir = _write_trace(tmp_path, [
        {"type": "human", "content": "do it"},
        {"type": "ai", "content": "ok", "tool_calls": ["write_file"]},
    ])
    objs = pregrade._load_trace_lines(run_dir)
    assert len(objs) == 2


def test_load_trace_lines_tolerates_bad_json(tmp_path):
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    p = run_dir / "trace.0.jsonl"
    p.write_text('{"type":"ai","content":"ok"}\nNOT JSON\n{"type":"human","content":"x"}\n')
    objs = pregrade._load_trace_lines(run_dir)
    assert len(objs) == 2  # bad line skipped


def test_load_trace_lines_empty_dir(tmp_path):
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    assert pregrade._load_trace_lines(run_dir) == []


# ---------------------------------------------------------------------------
# compute_kpis
# ---------------------------------------------------------------------------


def _make_run(tmp_path, ai_turns: list[dict]) -> Path:
    lines = [{"type": "human", "content": "task"}, *ai_turns]
    return _write_trace(tmp_path, lines)


def test_kpis_no_empty_turns(tmp_path):
    run_dir = _make_run(tmp_path, [
        {"type": "ai", "content": "", "tool_calls": ["write_file"]},
        {"type": "ai", "content": "done", "tool_calls": []},
    ])
    k = pregrade.compute_kpis(run_dir)
    assert k["n_ai_turns"] == 2
    assert k["empty_turn_rate"] == 0.0
    assert k["first_tool_turn"] == 1
    assert k["tool_calls_per_turn"] == 0.5


def test_kpis_all_empty(tmp_path):
    run_dir = _make_run(tmp_path, [
        {"type": "ai", "content": "", "tool_calls": []},
        {"type": "ai", "content": "", "tool_calls": []},
    ])
    k = pregrade.compute_kpis(run_dir)
    assert k["empty_turn_rate"] == 1.0
    assert k["first_tool_turn"] is None


def test_kpis_no_ai_turns(tmp_path):
    run_dir = _write_trace(tmp_path, [{"type": "human", "content": "hi"}])
    k = pregrade.compute_kpis(run_dir)
    assert k["n_ai_turns"] == 0
    assert k["empty_turn_rate"] is None
    assert k["tool_calls_per_turn"] is None


def test_kpis_first_tool_turn_correct(tmp_path):
    run_dir = _make_run(tmp_path, [
        {"type": "ai", "content": "thinking", "tool_calls": []},
        {"type": "ai", "content": "", "tool_calls": []},
        {"type": "ai", "content": "", "tool_calls": ["write_file"]},
    ])
    k = pregrade.compute_kpis(run_dir)
    assert k["first_tool_turn"] == 3  # 1-based


# ---------------------------------------------------------------------------
# failure_tag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason,expected", [
    ("max_turns_reached", "action"),
    ("loop_detected", "action"),
    ("stalled_no_progress", "action"),
    ("timeout_60s", "context"),
    ("context_exceeded", "context"),
    ("tool_error_404", "salvage"),
    ("verify_failed", "verify"),
    ("blocker_declared", "recovery"),
    ("cancelled_by_user", "recovery"),
    ("something_else", "action"),
    (None, "unknown"),
    ("", "unknown"),
])
def test_failure_tag(reason, expected):
    assert pregrade.failure_tag(reason) == expected


# ---------------------------------------------------------------------------
# listed_path_coverage
# ---------------------------------------------------------------------------


def test_coverage_full_match():
    assert pregrade.listed_path_coverage(["a.py", "b.py"], ["a.py", "b.py"]) == 1.0


def test_coverage_partial():
    assert pregrade.listed_path_coverage(["a.py", "b.py"], ["a.py"]) == 0.5


def test_coverage_no_match():
    assert pregrade.listed_path_coverage(["a.py"], ["c.py"]) == 0.0


def test_coverage_none_when_empty_expected():
    assert pregrade.listed_path_coverage([], ["a.py"]) is None


def test_coverage_none_when_files_changed_unknown():
    assert pregrade.listed_path_coverage(["a.py"], None) is None
