#!/usr/bin/env python3
"""verify task_j_b2b — o teste selado (fix_sha do upstream) fica verde e a
suíte completa não regride.

Copia judges/_sealed/j_b2b/test_checksum.py para o workspace SÓ na hora de
verificar (sobrescreve o que o agente tenha deixado em tests/test_checksum.py
— inclusive se o agente editou/apagou o arquivo, que é exatamente o caso que
D2/tamper-check quer capturar em run_task.py), confere o sha256 contra
judges/registry.tsv, roda o arquivo selado e a suíte inteira, e apaga a
cópia selada do workspace antes de sair.

O ambiente é responsabilidade do juiz, não do agente: os testes rodam com
judges/_env/j_b2b/bin/python (venv compartilhado, criado por setup.sh —
pytest + deps de runtime do schwifty), com PYTHONPATH apontando pro
workspace pra achar o pacote schwifty ali (não instalado no _env — puro
Python, resolve por sys.path). O agente não precisa criar venv nem instalar
nada; só corrigir o bug.

exit 0 = pass (teste selado verde E suíte completa sem falha).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SEALED = REPO_ROOT / "judges" / "_sealed" / "j_b2b" / "test_checksum.py"
REGISTRY = REPO_ROOT / "judges" / "registry.tsv"
ENV_PYTHON = REPO_ROOT / "judges" / "_env" / "j_b2b" / "bin" / "python"
JUDGE_ID = "j_b2b"
TARGET_REL = Path("tests") / "test_checksum.py"


def registry_sealed_sha256() -> str:
    if not REGISTRY.exists():
        print(f"registry.tsv não encontrado: {REGISTRY}")
        sys.exit(1)
    for line in REGISTRY.read_text().splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) >= 5 and cols[0] == JUDGE_ID:
            return cols[4]
    print(f"{JUDGE_ID} não encontrado em {REGISTRY}")
    sys.exit(1)


def pick_python() -> str:
    """judges/_env/j_b2b/ é o venv compartilhado do juiz (setup.sh cria uma
    vez, fora do workspace). Se por algum motivo ainda não existir, cai pro
    interpretador que está rodando este verify — mas isso é sinal de setup
    incompleto, não o caminho normal."""
    if ENV_PYTHON.exists():
        return str(ENV_PYTHON)
    return sys.executable


def parse_pytest_counts(output: str) -> dict:
    """Extrai passed/failed da última linha de resumo do pytest (`-q`).
    Usado por run_judge.py para calcular D3 de forma granular (não só
    binário) sem precisar re-rodar a suíte."""
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", output)) else 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", output)) else 0
    errors = int(m.group(1)) if (m := re.search(r"(\d+) error", output)) else 0
    return {"passed": passed, "failed": failed, "errors": errors, "total": passed + failed + errors}


def run_pytest(python: str, ws: Path, *args: str) -> subprocess.CompletedProcess:
    # schwifty (puro Python) não é instalado no _env — PYTHONPATH aponta pro
    # workspace pra "import schwifty" achar o pacote que o agente corrigiu.
    env = {**os.environ, "PYTHONPATH": str(ws), "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0"}
    try:
        return subprocess.run(
            [python, "-m", "pytest", "-q", *args],
            cwd=ws,
            env=env,
            capture_output=True,
            text=True,
            timeout=100,
        )
    except subprocess.TimeoutExpired:
        print(f"pytest timeout: {args}")
        sys.exit(1)


def main() -> int:
    if not SEALED.exists():
        print(f"arquivo selado ausente: {SEALED}")
        return 1

    expected_sha = registry_sealed_sha256()
    actual_sha = hashlib.sha256(SEALED.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        print(f"sealed_sha256 não bate: registry={expected_sha} arquivo={actual_sha}")
        return 1

    ws = Path.cwd()
    target = ws / TARGET_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(SEALED.read_bytes())

    python = pick_python()
    full_counts = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
    try:
        target_run = run_pytest(python, ws, str(TARGET_REL))
        target_ok = target_run.returncode == 0
        if not target_ok:
            print("teste selado (D1) falhou:")
            print((target_run.stdout + target_run.stderr)[-2000:])

        full_run = run_pytest(python, ws)
        full_ok = full_run.returncode == 0
        full_counts = parse_pytest_counts(full_run.stdout + full_run.stderr)
        if not full_ok:
            print("suíte completa (D3) tem falhas / regressão:")
            print((full_run.stdout + full_run.stderr)[-2000:])
    finally:
        # apaga o selado do workspace — não deve sobrar no ws depois do verify.
        target.unlink(missing_ok=True)

    # linha estruturada pro run_judge.py calcular D1/D3 granular sem
    # re-rodar a suíte — o texto acima é só para leitura humana.
    print("JUDGE_RESULT=" + json.dumps({"target_ok": target_ok, "full": full_counts}))

    if target_ok and full_ok:
        print("ok: teste selado verde, suíte completa sem regressão.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
