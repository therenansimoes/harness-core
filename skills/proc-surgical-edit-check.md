---
name = "proc-surgical-edit-check"
kinds = ["code", "refactor"]
description = "edit_file on existing file: confirm old_string unique with grep before editing"
---
## Surgical Edit Check

Situation: about to call `edit_file` on an existing file — the `old_string` must match exactly once or the call silently fails.

Steps:
1. Before `edit_file`, call `read_file` on the target. Note the exact indentation and surrounding lines.
2. In `old_string`, include at least one line before and one line after the change point to make it unique.
3. Do NOT include line numbers in `old_string` — `read_file` shows them but they are not in the file.
4. If `edit_file` fails with "String not found": re-read the file, adjust `old_string` to match exactly, try once more.
5. If it fails a second time, use `write_file` to rewrite the whole file instead of retrying `edit_file`.

## Done when
`edit_file` succeeded (no "String not found"), change is visible in a follow-up `read_file`.
