---
name = "proc-rename-via-write"
kinds = ["code", "refactor"]
description = "rename symbol across listed .py paths: read all files first then edit all before stopping"
paths = ["**/*.py"]
---
## Rename Via Write

Situation: task asks to rename a symbol (function, class, variable) across one or more `.py` files.

Steps:
1. Call `read_file` on EVERY path listed in the spec — do all reads in the same turn if possible.
2. For each file that contains the old name, call `edit_file` replacing the old symbol with the new one. Check that `old_string` contains enough context (≥1 surrounding line) to be unique.
3. If `edit_file` fails with "String not found", re-read the file and adjust `old_string` to match exactly (no line numbers, exact indentation).
4. Do NOT stop after editing the first file. Continue until ALL listed paths are updated.
5. After all edits, run the verify command (`pytest -q` or equivalent) to confirm no broken references.

## Done when
Every listed `.py` path has the new symbol name, verify command exits 0.
