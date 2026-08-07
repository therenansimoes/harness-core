# Tools (Qwopus local)

File tools use the **virtual workspace root**. Paths look like `/util.py` —
never invent prefixes (`/p/`, `/tmp/`, `/Users/…`). `ls` first if unsure.

## ls
List `/` and subdirs.

## read_file
`read_file(file_path="/util.py")` before every edit. Do not copy line numbers into `edit_file`.

## write_file
New or full rewrite of small files: `write_file(file_path="/x.py", content=...)`.

## edit_file
Surgical patches. Rename: read ALL listed paths, then edit/write EVERY hit until `grep` is clean — never end a turn empty with no tool_call while edits remain.

## glob
Find paths by pattern.

## grep
Search contents (confirm rename done).

## execute
cwd is workspace — relative paths only (`util.py`). `/` in the shell is the machine root.

## delete
Only when required.

## task
Skip planner/reviewer when ≤3 files are fully specified or the job is a listed rename.

## Done
Run verify / paste real exit evidence. Never invent a green.
