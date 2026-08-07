# Tools (local / openai-compat)

Use tools. Prefer reading before writing. Diff minimum.
Virtual file paths: `/x.py` (never invent `/p/` prefixes). `execute` uses relative paths.

## ls
List workspace root / dirs.

## read_file
`read_file(file_path="/x.py")` before every edit. Line numbers in output are not part of the file.

## write_file
New files only: `write_file(file_path="/x.py", content=...)`.

## edit_file
Exact `old_string` → `new_string`. Rename = edit every listed call site (or rewrite small files with write_file).

## glob
Find paths by pattern.

## grep
Search file contents.

## execute
Shell in workspace cwd — relative paths only. Paste real exit/stderr.

## delete
Only when the task requires removing a file.

## task
`task(subagent_type="planner"|"reviewer")` — skip when ≤3 files are fully specified or the job is a listed rename.

## Done
Run verify before claiming success. Never invent a green.
