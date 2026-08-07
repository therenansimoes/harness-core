# Procedure index
Load body when situation matches. ≤2 procedures at a time.

| name | trigger |
|---|---|
| proc-recover-after-empty-turn | nudge fires with `[empty_turn]` — last response had no tool call |
| proc-verify-then-stop | about to claim task done — run verify_cmd first |
| proc-exact-write-from-spec | EXACT / EXATAMENTE / spec gives literal file content to write |
| proc-recover-missing-files | nudge fires with `[completion_guard]` — expected files missing |
| proc-rename-via-write | rename symbol across listed `.py` paths |
| proc-two-module-create | create importer + imported `.py` module together |
| proc-surgical-edit-check | `edit_file` on existing file — confirm `old_string` unique first |
| proc-declare-blocker | stuck ≥2 attempts, same error, no forward progress |
| proc-check-listed-files | verify step — confirm every path in spec exists before stopping |
| proc-content-cta-skeleton | CTA / marketing / inventory / product content output task |
