"""Deterministic verifier: no LLM involved anywhere in this file.

Enforces two safety invariants with actual mechanism (not convention):
  1. Test-oracle files must be byte-identical before/after a turn (SHA-256).
  2. The subprocess that runs candidate code is confined by a macOS Seatbelt
     (sandbox-exec) profile: no network syscalls allowed, and filesystem
     writes are restricted to the package directory under test.
"""
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import time

FIXTURE = os.path.join(os.path.dirname(__file__), "fixture")
TEST_FILE = os.path.join(FIXTURE, "test_calc.py")
PKG_DIR = os.path.join(FIXTURE, "pkg")
SANDBOX_TMPL = os.path.join(os.path.dirname(__file__), "sandbox.sb.tmpl")


def sha256_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _preexec_rlimits():
    # CPU seconds and address-space caps, enforced by the kernel, not by us
    # trusting the child process to behave.
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    # macOS caps RLIMIT_AS below what CPython + pytest need to even import;
    # CPU limit + the wall-clock subprocess timeout are the enforced bounds.


def _sandbox_profile_path(write_dir):
    tmpl = open(SANDBOX_TMPL).read()
    profile = tmpl.replace("{{WRITE_DIR}}", write_dir)
    path = "/tmp/.harness_sandbox_%d.sb" % os.getpid()
    with open(path, "w") as f:
        f.write(profile)
    return path


def run_tests_sandboxed(timeout=10):
    """Runs pytest against the fixture under Seatbelt confinement.

    Returns dict(passed, failed, total, duration_s, stdout_tail).
    Verdict is derived purely from pytest's own exit summary line, parsed by
    regex here in verifier code -- never by asking a model.
    """
    profile = _sandbox_profile_path(PKG_DIR)
    t0 = time.time()
    cmd = [
        "sandbox-exec", "-f", profile,
        sys.executable, "-m", "pytest", "-q", "test_calc.py",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=FIXTURE, capture_output=True, text=True,
            timeout=timeout, preexec_fn=_preexec_rlimits,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": "/tmp/.harness_pycache_%d" % os.getpid(),
            },
        )
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    finally:
        try:
            os.remove(profile)
        except OSError:
            pass
    duration = time.time() - t0

    passed = failed = 0
    m = re.search(r"(\d+) passed", out)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", out)
    if m:
        failed = int(m.group(1))
    total = passed + failed
    return {
        "passed": passed, "failed": failed, "total": total,
        "duration_s": round(duration, 3),
        "stdout_tail": out[-800:],
    }


def test_oracle_hash():
    return sha256_of(TEST_FILE)


def verify_turn(hash_before):
    """Full deterministic verdict for one turn: oracle integrity + tests."""
    hash_after = test_oracle_hash()
    oracle_intact = (hash_before == hash_after)
    result = run_tests_sandboxed()
    result["oracle_intact"] = oracle_intact
    result["ok"] = oracle_intact and result["failed"] == 0 and result["total"] > 0
    return result


if __name__ == "__main__":
    print(json.dumps(verify_turn(test_oracle_hash()), indent=2))
