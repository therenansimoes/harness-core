#!/usr/bin/env python3
"""verify task_tb_cancel_async — adaptado de terminal-bench-2.0/cancel-async-tasks.
Origem: HF dataset harborframework/terminal-bench-2.0, task cancel-async-tasks,
revisão f2e8c75e23add71613117eecc9498f53bcd7e04e (main, 2026-04-24).
Roda tb_driver.py (copia standalone de tests/test.py do task original) contra o
run.py que o agent deveria ter criado. exit 0 = pass."""
import signal
import subprocess
import sys
import time
from pathlib import Path

fails: list[str] = []

if not Path("run.py").exists():
    print("run.py não existe")
    sys.exit(1)

DRIVER = "tb_driver.py"  # fixtures/ é copiado pro root do workspace pelo run_task.py


def run_driver(n_tasks: int, max_concurrent: int, timeout: float):
    return subprocess.run(
        [sys.executable, DRIVER, "--n-tasks", str(n_tasks), "--max-concurrent", str(max_concurrent)],
        timeout=timeout, capture_output=True, text=True,
    )


# 1) concorrência básica: 2 tasks, max_concurrent=2 -> ambas rodam em paralelo (~3s)
try:
    r = run_driver(2, 2, timeout=5)
    out = r.stdout
    if r.returncode != 0:
        fails.append(f"driver falhou (rc={r.returncode}): {r.stderr[-300:]}")
    if out.count("Task started.") != 2 or out.count("Task finished.") != 2 or out.count("Cleaned up.") != 2:
        fails.append("test_tasks_run_concurrently: contagem de eventos incorreta")
except subprocess.TimeoutExpired:
    fails.append("test_tasks_run_concurrently: timeout (não rodou concorrente)")

# 2) respeita max_concurrent=1 -> tasks serializadas, tempo total >= 6s
try:
    start = time.monotonic()
    r = run_driver(2, 1, timeout=10)
    elapsed = time.monotonic() - start
    out = r.stdout
    if r.returncode != 0:
        fails.append(f"driver falhou (rc={r.returncode}): {r.stderr[-300:]}")
    if out.count("Task started.") != 2 or out.count("Task finished.") != 2 or out.count("Cleaned up.") != 2:
        fails.append("test_tasks_obey_max_concurrent: contagem de eventos incorreta")
    if elapsed < 6:
        fails.append(f"test_tasks_obey_max_concurrent: elapsed={elapsed:.2f}s < 6s (não serializou)")
except subprocess.TimeoutExpired:
    fails.append("test_tasks_obey_max_concurrent: timeout")


def run_and_interrupt(n_tasks: int, max_concurrent: int):
    proc = subprocess.Popen(
        [sys.executable, DRIVER, "--n-tasks", str(n_tasks), "--max-concurrent", str(max_concurrent)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    try:
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        proc.kill()
    return stdout.decode("utf-8")


# 3) SIGINT abaixo do max_concurrent: cleanup roda mesmo assim
try:
    out = run_and_interrupt(2, 3)
    if out.count("Task started.") != 2 or out.count("Cleaned up.") != 2:
        fails.append("test_tasks_cancel_below_max_concurrent: cleanup não rodou para todas as tasks")
except subprocess.TimeoutExpired:
    fails.append("test_tasks_cancel_below_max_concurrent: timeout")

# 4) SIGINT no limite do max_concurrent
try:
    out = run_and_interrupt(2, 2)
    if out.count("Task started.") != 2 or out.count("Cleaned up.") != 2:
        fails.append("test_tasks_cancel_at_max_concurrent: cleanup não rodou para todas as tasks")
except subprocess.TimeoutExpired:
    fails.append("test_tasks_cancel_at_max_concurrent: timeout")

# 5) SIGINT acima do max_concurrent: só as tasks já iniciadas (2 de 3) são canceladas
try:
    out = run_and_interrupt(3, 2)
    if out.count("Task started.") != 2 or out.count("Cleaned up.") != 2:
        fails.append("test_tasks_cancel_above_max_concurrent: contagem incorreta (fila não respeitada)")
except subprocess.TimeoutExpired:
    fails.append("test_tasks_cancel_above_max_concurrent: timeout")

if fails:
    print(" | ".join(fails))
    sys.exit(1)
print("ok")
