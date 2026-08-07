---
name = "proc-recover-missing-files"
kinds = []
description = "completion_guard nudge fired listing missing files: write each listed file now"
---
## Recover Missing Files

Situation: middleware fired `[completion_guard]` with a list of expected files that do not exist yet. You stopped before writing all required outputs.

Steps:
1. Read the nudge message: it lists the exact missing paths.
2. For each missing path, call `write_file` with the correct content derived from the spec.
3. Write ALL missing files before sending any text response.
4. Do not write placeholder content ("# TODO") — write the real content the spec requires.
5. After writing all files, run the verify command or `ls` to confirm all paths now exist.

Anti-pattern: explaining why the file is missing instead of writing it.

## Done when
Every path listed in the `[completion_guard]` nudge now exists with real content.
