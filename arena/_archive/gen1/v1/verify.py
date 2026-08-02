"""Deterministic verifier: code decides pass/fail, never an LLM opinion."""
import subprocess
from pathlib import Path


def run_tests(task_dir: Path, timeout=30):
    task_dir = Path(task_dir)
    proc = subprocess.run(
        ["python3", "-m", "pytest", "-q", str(task_dir)],
        cwd=task_dir, capture_output=True, text=True, timeout=timeout,
    )
    passed = proc.returncode == 0
    tail = (proc.stdout + proc.stderr)[-3000:]
    n_passed = tail.count(" passed")
    return {"passed": passed, "returncode": proc.returncode, "output_tail": tail}
