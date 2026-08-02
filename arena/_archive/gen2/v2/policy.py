"""Policy: proposes the next action given task state.

Two backends:
  - anthropic: real Claude call (used only if ANTHROPIC_API_KEY is set and the
    `anthropic` package is importable). Real cost/tokens are read off the
    response and returned so the trace can carry a genuine USD figure.
  - offline: deterministic, no network, no API key, NO hint comments. It
    performs a bounded local mutation search over operators/comparators and
    small integer literals: for every candidate line it tries a single-site
    mutation, reruns the real test command, and keeps the mutation only if it
    strictly increases the number of passing tests versus the current file.
    This is a (tiny) real program-repair search, not a lookup of the answer —
    it has no knowledge of what the bug is, only of "did the test count go up".
"""
import os
import re
from pathlib import Path

from verify import run_tests
from tracelib import price_usd

BACKEND = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "offline"

_SWAPS = [
    ("+", "-"), ("-", "+"), ("*", "/"), ("/", "*"),
    (">=", "<="), ("<=", ">="), (">", "<"), ("<", ">"),
    ("==", "!="), ("!=", "=="),
    (" and ", " or "), (" or ", " and "),
]


def _count_passed(output: str) -> int:
    m = re.search(r"(\d+) passed", output)
    return int(m.group(1)) if m else 0


def propose_action(task_dir: Path, instruction: str, last_output: str, turn: int):
    if BACKEND == "anthropic":
        return _anthropic_action(task_dir, instruction, last_output)
    return _offline_action(task_dir, last_output, turn)


def _offline_action(task_dir: Path, last_output: str, turn: int):
    task_dir = Path(task_dir)
    baseline = run_tests(task_dir)
    baseline_n = _count_passed(baseline["output_tail"])

    candidates = sorted(p for p in task_dir.glob("*.py") if not p.name.startswith("test_"))
    for py in candidates:
        original = py.read_text()
        lines = original.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            tried = set()
            mutations = []
            for old, new in _SWAPS:
                if old in line:
                    mutations.append(line.replace(old, new, 1))
            for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line):
                n = int(m.group(1))
                for delta in (1, -1):
                    mutated = line[: m.start()] + str(n + delta) + line[m.end():]
                    mutations.append(mutated)

            for mutated_line in mutations:
                if mutated_line == line or mutated_line in tried:
                    continue
                tried.add(mutated_line)
                new_lines = list(lines)
                new_lines[i] = mutated_line
                candidate_text = "".join(new_lines)
                py.write_text(candidate_text)
                try:
                    result = run_tests(task_dir)
                finally:
                    py.write_text(original)  # never leave a trial mutation in place
                n_passed = _count_passed(result["output_tail"])
                if result["passed"] or n_passed > baseline_n:
                    return {
                        "type": "edit_file",
                        "path": str(py.relative_to(task_dir)),
                        "content": candidate_text,
                    }
        # restore just in case
        py.write_text(original)
    return {"type": "done"}


def _anthropic_action(task_dir: Path, instruction: str, last_output: str):
    import anthropic
    client = anthropic.Anthropic()
    files = {}
    for py in sorted(Path(task_dir).glob("*.py")):
        files[py.name] = py.read_text()
    prompt = (
        f"Task: {instruction}\n\nFiles:\n"
        + "\n".join(f"--- {n} ---\n{c}" for n, c in files.items())
        + f"\n\nLast test output:\n{last_output}\n\n"
        "Reply with ONLY a JSON object: "
        '{"type":"edit_file","path":"<file>","content":"<full new file content>"} '
        'or {"type":"done"} if tests should already pass.'
    )
    model = "claude-sonnet-4-5"
    resp = client.messages.create(
        model=model, max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
    cost_usd = price_usd(model, tokens_in, tokens_out)
    import json
    try:
        action = json.loads(text)
    except Exception:
        action = {"type": "done"}
    action["_tokens_in"] = tokens_in
    action["_tokens_out"] = tokens_out
    action["_cost_usd"] = cost_usd
    return action
