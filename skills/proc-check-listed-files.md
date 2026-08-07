---
name = "proc-check-listed-files"
kinds = []
description = "verify step before claiming done: ls or read_file every path listed in spec"
---
## Check Listed Files

Situation: about to declare the task complete — spec listed specific output paths and you have not confirmed all of them exist.

Steps:
1. Collect every file path mentioned in the spec (look for `.py`, `.json`, `.md`, `.toml` references).
2. For each path, call `read_file` or run `ls <path>` to confirm it exists and has real content.
3. If any path is missing: write it now (see `proc-recover-missing-files` if nudge already fired, else just write it).
4. If a path exists but has wrong content (placeholder, empty, wrong function names): fix it before stopping.
5. Only after ALL listed paths confirmed: run the verify command and stop.

## Done when
Every path listed in the spec confirmed to exist with correct content; verify command green.
