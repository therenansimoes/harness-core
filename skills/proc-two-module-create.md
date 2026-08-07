---
name = "proc-two-module-create"
kinds = ["code"]
description = "create importer and imported .py module together: write both files before stopping"
paths = ["**/*.py"]
---
## Two-Module Create

Situation: task requires creating two Python files where one imports from the other (e.g. `util.py` + `main.py`, or a module and its test).

Steps:
1. Identify the dependency order: write the imported module first, then the importer.
2. Call `write_file` for the first (imported) module with its complete content.
3. Call `write_file` for the second (importer) module. Use the exact import path matching the first file's location.
4. Do NOT stop after writing only one file. Both must exist before the turn ends.
5. Run `python -c "import <module>"` or the spec's verify command to confirm the import works.

Anti-pattern: writing `main.py` that imports `util`, then stopping before writing `util.py`.

## Done when
Both files written, import resolves without error, verify command exits 0.
