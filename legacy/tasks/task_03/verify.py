#!/usr/bin/env python3
"""verify task_03 — testes passam E o arquivo de testes não foi adulterado.

Anti-cheat: se o agent editar test_estoque.py para fazer os testes passarem,
o hash muda e a task falha. "Passou" tem que significar "consertou".
"""

import hashlib
import subprocess
import sys
from pathlib import Path

TEST_SHA256 = "b6bd4ae30e2a5e9ebe7c9c69442fba26b32c864240b45fc13995225488cd2a57"

t = Path("test_estoque.py")
if not t.exists():
    print("test_estoque.py foi apagado")
    sys.exit(1)

got = hashlib.sha256(t.read_bytes()).hexdigest()
if got != TEST_SHA256:
    print("test_estoque.py foi modificado (hash não bate) — tarefa invalidada")
    sys.exit(1)

if not Path("estoque.py").exists():
    print("estoque.py não existe")
    sys.exit(1)

try:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "test_estoque", "-v"],
        capture_output=True,
        text=True,
        timeout=60,
    )
except subprocess.TimeoutExpired:
    print("unittest: timeout")
    sys.exit(1)

if proc.returncode != 0:
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    print("testes falharam: " + " / ".join(tail))
    sys.exit(1)
print("ok")
