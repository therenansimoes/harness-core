---
name = "proc-recover-after-empty-turn"
kinds = []
description = "empty_turn nudge fired: write_file or edit_file immediately, no explanation"
---
## Recover After Empty Turn

Situation: middleware fired `[empty_turn]` — your last response had no tool call and no file was written. You are about to loop or stop without output.

Steps:
1. Do NOT send another explanation. The task is not done.
2. Re-read the spec (the last user message): what file(s) must be created or changed?
3. Call `write_file` or `edit_file` RIGHT NOW with the correct content. One tool call per file.
4. If you genuinely cannot determine what to write (missing spec detail), call `declare_blocker` with the exact missing detail. Do not send empty text again.
5. After writing, stop — do not send a follow-up message describing what you did.

## Done when
`write_file` or `edit_file` was called with the expected output file — no blank response.
