---
name = "proc-declare-blocker"
kinds = []
description = "stuck 2+ attempts no progress: declare_blocker immediately do not retry"
---
## Declare Blocker

Situation: you have attempted the same action ≥2 times without forward progress — the same error repeats, required information is missing, or the path forward is genuinely ambiguous.

Steps:
1. Stop retrying. One more attempt will not fix a structural blocker.
2. Call `declare_blocker` with:
   - `blocker_type`: one of `missing_info`, `tool_failure`, `spec_ambiguous`, `environment_error`
   - `description`: one sentence — what you tried, what failed, what is needed to unblock
3. Do NOT send a long explanation. One sentence in the blocker description is enough.
4. Do NOT attempt a workaround that writes incorrect output just to avoid blocking.

Anti-pattern: retrying 5 times with slight variations hoping it works — this burns turns and produces bad output.

## Done when
`declare_blocker` called once with a clear description; no further retry attempts.
