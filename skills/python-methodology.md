---
name = "python-methodology"
kinds = ["code", "refactor"]
description = "Python coding: typed pytest uv json fixtures out.json dados.py rename soma_total calcula_total write_file full file two modules verify"
paths = ["**/*.py", "*.py", "**/fixtures/*.json", "out.json"]
---
## Python methodology

- Prefer `uv run` / project env; do not invent global installs.
- Read the failing pytest or traceback first; fix the cause, not the message.
- Keep public functions typed; avoid `Any` unless the call site already uses it.
- Diff minimum: no drive-by renames, no reformatting neighbors.
- JSON micros: `read_file` the fixture, compute, `write_file` `out.json` at workspace root.
- Two-module create: write every listed `.py` (importer + imported) before stopping — never only one of them.
- Rename (small .py): after one `read_file`, `write_file` the FULL file with the new name everywhere — do not chain edit_file then stop. Grep until the old name is gone in every listed path.
- Tests: add or extend a focused test when behavior changes; never delete asserts to go green.
- After edits: run the unit's verify_cmd (usually pytest) and paste the real exit evidence.

## Done when
- verify_cmd (pytest) run and output pasted; old symbol absent from every listed path; no drive-by renames or reformatting.
