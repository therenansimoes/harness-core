---
name = "proc-verify-then-stop"
kinds = []
description = "verify_cmd run required before claiming done: paste real output then stop"
---
## Verify Then Stop

Situation: you believe the task is complete and are about to declare it done.

Steps:
1. Do NOT say "done" before running the verify command.
2. Run the verify command from the spec (e.g. `pytest -q`, `node index.js`, `python script.py`). If no verify command is specified, run `ls` on the expected output paths.
3. Paste the real output verbatim — do not summarize or paraphrase.
4. If the output is green (exit 0, expected content), stop. Task complete.
5. If the output is red, report the real failure and fix it. Do not claim "passed" with a red result.

Anti-pattern: "I wrote the file, it should work." — this is not a verify. Run the command.

## Done when
Verify command ran, real output pasted, exit code is 0 (or expected files confirmed to exist).
