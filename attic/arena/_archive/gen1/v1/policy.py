"""Policy: proposes the next action given task state.

Two backends:
  - anthropic: real Claude call (used if ANTHROPIC_API_KEY is set and the
    `anthropic` package is importable). Sends task instructions + last test
    output, asks for one shell command or a file edit as the next action.
  - offline: deterministic, no network, no API key. Scans for `# BUG:` repair
    hints left in the task source and fixes the matching line. It's a stub
    standing in for "propose a patch" so the rest of the loop (act -> verify
    -> trace -> stop-on-green) is exercisable end-to-end without any credentials.
"""
import os
import re
from pathlib import Path

BACKEND = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "offline"


def propose_action(task_dir: Path, instruction: str, last_output: str, turn: int):
    if BACKEND == "anthropic":
        return _anthropic_action(task_dir, instruction, last_output)
    return _offline_action(task_dir, last_output, turn)


def _offline_action(task_dir: Path, last_output: str, turn: int):
    """Find first `# BUG: should be <expr>` hint and patch it. Deterministic."""
    task_dir = Path(task_dir)
    for py in sorted(task_dir.glob("*.py")):
        if py.name.startswith("test_"):
            continue
        text = py.read_text()
        m = re.search(r"^(.*)#\s*BUG:\s*should be (.+)$", text, re.MULTILINE)
        if m:
            line_prefix, fix_expr = m.group(1), m.group(2).strip()
            old_line = m.group(0)
            lhs = line_prefix.split("=", 1)[0] if "return" not in line_prefix else line_prefix.split("return", 1)[0] + "return "
            new_line = f"{lhs}{fix_expr}"
            new_text = text.replace(old_line, new_line)
            return {"type": "edit_file", "path": str(py.relative_to(task_dir)), "content": new_text}
    return {"type": "done"}


def _anthropic_action(task_dir: Path, instruction: str, last_output: str):
    import anthropic
    client = anthropic.Anthropic()
    files = {}
    for py in sorted(Path(task_dir).glob("*.py")):
        files[py.name] = py.read_text()
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
    text = resp.content[0].text
    import json
    try:
        return json.loads(text)
    except Exception:
        return {"type": "done"}
