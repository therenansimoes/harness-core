---
name = "proc-exact-write-from-spec"
kinds = []
description = "EXACT EXATAMENTE new file from spec: write_file once with full content"
---
## Exact Write From Spec

Situation: spec says write a new file with exact content (keywords: EXACT, EXATAMENTE, "write exactly", or spec provides literal content to copy).

Steps:
1. Read the spec carefully — copy the exact content, variable names, and structure as given. No improvisation.
2. Call `write_file` once with the complete file content. Do not split across multiple calls.
3. Do not add imports, comments, or extra lines not in the spec.
4. After writing, run `read_file` on the new path to confirm it was written correctly.
5. Run the verify command (if given). If none, confirm the path exists with `ls`.

Note: Protocol 0 — when spec is unambiguous and content is fully specified, skip planning. Write directly.

## Done when
`write_file` called once, content matches spec exactly, path confirmed to exist.
