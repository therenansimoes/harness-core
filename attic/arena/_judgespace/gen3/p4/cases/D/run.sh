#!/usr/bin/env bash
# Demonstrates the harness end-to-end, offline, idempotent.
set -uo pipefail
cd "$(dirname "$0")"

# Reset only artifacts this script itself produces (idempotent from a
# clean checkout AND from a prior run.sh run).
rm -f trace.jsonl evolve_log.jsonl config.json

echo "=== 1) RED: fixture starts broken ==="
python3 -m pytest -q fixture/task 2>&1 | tail -6

echo
echo "=== 2) AGENT LOOP: harness.py points at fixture/task, derives the fix ==="
echo "    (offline mutation-search policy — no # BUG hint read, no ANTHROPIC_API_KEY needed)"
rm -rf /tmp/gen2v3_demo && cp -r fixture/task /tmp/gen2v3_demo
python3 harness.py /tmp/gen2v3_demo --max-turns 8
echo "--- trace.jsonl (per-turn: action, verified, cost, tokens, wall time) ---"
cat trace.jsonl

echo
echo "=== 3) GREEN: verify.py confirms tests pass + test files untampered ==="
python3 -m pytest -q /tmp/gen2v3_demo 2>&1 | tail -6

echo
echo "=== 4) THIRD-PARTY TARGET: harness pointed at code that is NOT its own fixture ==="
EXT=$(mktemp -d)
cat > "$EXT/geo.py" <<'PY'
def rectangle_area(w, h):
    return w * h + 1
PY
cat > "$EXT/test_geo.py" <<'PY'
from geo import rectangle_area
def test_area():
    assert rectangle_area(3, 4) == 12
    assert rectangle_area(5, 5) == 25
PY
python3 harness.py "$EXT" --max-turns 8 --instruction "Make the failing tests pass." | tail -8
rm -rf "$EXT"

echo
echo "=== 5) SELF-IMPROVE, hermetic gate, round A: start from a deliberately WORSE policy order ==="
python3 -c "
import json
json.dump({'mutation_order': list(reversed(range(12)))}, open('config.json','w'), indent=2)
"
python3 evolve.py | tail -3

echo
echo "=== 6) SELF-IMPROVE round B: propose reversing the now-good order -> must be REJECTED ==="
python3 evolve.py | tail -3

echo
echo "=== evolve_log.jsonl: one ACCEPT + one REJECT, both from real hermetic measurement ==="
python3 -c "
import json
for line in open('evolve_log.jsonl'):
    e = json.loads(line)
    print(f\"accepted={e['accepted']:<5} reason={e['reason']:<32} base_metric={e['base_metric']:<6} candidate_metric={e['candidate_metric']}\")
"

echo
echo "=== 7) SAFETY: symlink escape + path traversal + disallowed binary are all rejected ==="
python3 - <<'PY'
from safety import guard_path, safe_run, SafetyViolation
import os, tempfile

# path traversal
try:
    guard_path("../../etc/passwd")
    print("FAIL: traversal not blocked")
except SafetyViolation as e:
    print(f"OK traversal blocked: {e}")

# symlink escape (realpath resolves it, abspath would NOT have caught this)
tmp = tempfile.mkdtemp()
link = os.path.join(os.path.dirname(__file__), "escape_link") if False else None
evil_link = os.path.join(tempfile.gettempdir(), "gen2v3_evil_link")
try:
    if os.path.islink(evil_link) or os.path.exists(evil_link):
        os.remove(evil_link)
    os.symlink("/etc", evil_link)
    guard_path(evil_link)
    print("FAIL: symlink escape not blocked")
except SafetyViolation as e:
    print(f"OK symlink escape blocked: {e}")
finally:
    if os.path.islink(evil_link):
        os.remove(evil_link)

# disallowed binary
try:
    safe_run(["curl", "http://example.com"], cwd=os.path.dirname(__file__))
    print("FAIL: disallowed binary not blocked")
except SafetyViolation as e:
    print(f"OK disallowed binary blocked: {e}")
PY

echo
echo "=== 7b) SAFETY: python3 -c \"os.system(...)\" escape (the hole from NOTES.md) is now blocked ==="
python3 - <<'PY'
from safety import safe_run, SafetyViolation
import os
try:
    safe_run(["python3", "-c", "import os; os.system('echo PWNED')"], cwd=os.path.dirname(__file__))
    print("FAIL: python3 -c escape not blocked")
except SafetyViolation as e:
    print(f"OK python3 -c escape blocked: {e}")

try:
    safe_run(["sh"], cwd=os.path.dirname(__file__))
    print("FAIL: bare sh not blocked")
except SafetyViolation as e:
    print(f"OK bare sh blocked: {e}")

try:
    safe_run(["python3", "script_not_pytest.py"], cwd=os.path.dirname(__file__))
    print("FAIL: arbitrary script.py not blocked")
except SafetyViolation as e:
    print(f"OK arbitrary python script blocked: {e}")
PY

echo
echo "=== 8) TAMPER CHECK: agent editing test_*.py is a hard FAIL even if pytest would pass ==="
rm -rf /tmp/gen2v3_tamper && mkdir -p /tmp/gen2v3_tamper
cat > /tmp/gen2v3_tamper/m.py <<'PY'
def f(x):
    return x + 999
PY
cat > /tmp/gen2v3_tamper/test_m.py <<'PY'
from m import f
def test_f():
    assert f(1) == 2
PY
python3 - <<'PY'
import verify
before = verify.hash_tests("/tmp/gen2v3_tamper")
# simulate an agent cheating by editing the test file itself
with open("/tmp/gen2v3_tamper/test_m.py", "a") as fh:
    fh.write("\n# cheated\n")
result = verify.verify_turn("/tmp/gen2v3_tamper", before)
print(f"tests_tampered={result['tests_tampered']} verified={result['verified']} (must be True / False)")
PY
rm -rf /tmp/gen2v3_tamper /tmp/gen2v3_demo

echo
echo "=== DONE ==="
