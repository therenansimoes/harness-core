"""Policy: proposes the next code edit given a failing test suite.

Two backends, chosen automatically (no API key -> offline, matches the
briefing's "run.sh must not depend on a key" requirement):

  - anthropic: real Claude call, used only if ANTHROPIC_API_KEY is set.
  - offline: a generic MUTATION SEARCH over the non-test .py files in the
    task dir. It does NOT read any "# BUG: should be X" hint and does NOT
    know the specific bug in advance (gen1's shared failure mode). It
    enumerates a fixed catalog of small syntactic mutations (flip a
    comparison operator, change +/-1 offsets, swap +/-, */ , and/or),
    applies each candidate to a scratch copy, and hands back the first
    mutation whose *caller* reports tests green. The harness does the
    actual hermetic verification — this module only proposes candidates.
    This is textbook mutation-testing-in-reverse: instead of mutating to
    break passing tests, we mutate to fix failing ones. It generalizes to
    any single-operator/off-by-one bug, not just the one seeded here.
"""
import itertools
import os
import re
from pathlib import Path

BACKEND = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "offline"

MUTATIONS = [
    (r"\-\s*1\b", ""),
    (r"\+\s*1\b", ""),
    (r"(?<![-+])\b(\w[\w.]*)\s\+\s(\w[\w.]*)\b", r"\1 - \2"),
    (r"(?<![-+])\b(\w[\w.]*)\s\-\s(\w[\w.]*)\b", r"\1 + \2"),
    (r"<=", "<"),
    (r"(?<!<)<(?!=)", "<="),
    (r">=", ">"),
    (r"(?<!>)>(?!=)", ">="),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\band\b", "or"),
    (r"\bor\b", "and"),
]


def _candidate_files(task_dir: Path):
    return [p for p in sorted(Path(task_dir).glob("*.py")) if not p.name.startswith("test_")]


def offline_candidates(task_dir, order=None):
    """Yield (path, new_content) candidate patches, one mutation at a time,
    applied once per occurrence per file — deterministic, bounded, no
    knowledge of which line is wrong. `order` (list of indices into
    MUTATIONS) lets evolve.py's self-improve loop propose a different
    search order as a candidate policy and measure it hermetically."""
    task_dir = Path(task_dir)
    mutations = [MUTATIONS[i] for i in order] if order else MUTATIONS
    for py in _candidate_files(task_dir):
        text = py.read_text()
        for pattern, repl in mutations:
            for m in re.finditer(pattern, text):
                start, end = m.span()
                new_text = text[:start] + re.sub(pattern, repl, text[start:end], count=1) + text[end:]
                if new_text != text:
                    yield str(py.relative_to(task_dir)), new_text


def propose_action(task_dir, instruction, last_output, turn, tried=None, order=None):
    if BACKEND == "anthropic":
        return _anthropic_action(task_dir, instruction, last_output)
    tried = tried or set()
    for i, (rel_path, content) in enumerate(offline_candidates(task_dir, order=order)):
        key = (rel_path, hash(content))
        if key in tried:
            continue
        return {"type": "edit_file", "path": rel_path, "content": content, "candidate_id": key}
    return {"type": "done"}


def _anthropic_action(task_dir, instruction, last_output):
    import anthropic
    client = anthropic.Anthropic()
    files = {p.name: p.read_text() for p in _candidate_files(Path(task_dir))}
    prompt = (
        f"Task: {instruction}\n\nFiles:\n" +
        "\n".join(f"--- {n} ---\n{c}" for n, c in files.items()) +
        f"\n\nLast test output:\n{last_output}\n\n"
        "Reply with ONLY a JSON object: "
        '{"type":"edit_file","path":"<file>","content":"<full new file content>"} '
        'or {"type":"done"} if tests should already pass.'
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    import json
    try:
        action = json.loads(resp.content[0].text)
    except Exception:
        action = {"type": "done"}
    action["usage"] = usage
    return action
