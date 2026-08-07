---
name = "method-planning"
kinds = []
description = "planning methodology: decompose order risks verify — skip plan when ≤3 files already specified with code"
---
## Plan before you implement

- Cap the plan at 6 steps. Each step names one path or command.
- Order by dependency: discover → change → verify. No parallel edits that touch the same file.
- Call out the riskiest step and how you will detect failure (verify_cmd, test name, HTTP check).
- Skip planning when ≤3 files are fully specified (code fences / EXACT) or the job is a named rename across listed paths — write/edit immediately.
- If the task fits one file, say so in one line and skip the rest.
- Use `write_todos` only when you actually planned; mark one item `in_progress` at a time.
- Re-plan when verify fails with a new root cause — do not silently enlarge scope.

## Done when
- Plan capped at 6 steps with named paths, riskiest step called out, verify_cmd identified, or task confirmed as single-file direct write.
