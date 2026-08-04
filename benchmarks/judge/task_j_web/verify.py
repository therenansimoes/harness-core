#!/usr/bin/env python3
"""verify task_j_web — o teste selado (fix_sha do upstream) fica verde e a
suíte completa não regride.

Copia judges/_sealed/j_web/index.test.ts para o workspace SÓ na hora de
verificar (sobrescreve o que o agente tenha deixado em
listen-keys/index.test.ts — inclusive se o agente editou/apagou o arquivo),
confere o sha256 contra judges/registry.tsv, roda o arquivo selado e a
suíte inteira (node test runner via `bnt`), e apaga a cópia selada do
workspace antes de sair.

Diferente do j_b2b: aqui não existe um venv/interpretador compartilhado
fora do workspace. node_modules/ é provisionado dentro do próprio
workspace (cópia de fixtures/, ver setup.sh) porque a resolução de módulos
do Node não tem um equivalente direto a PYTHONPATH apontando pra fora — o
agente não precisa instalar nada, só corrigir o bug em listen-keys/index.js.

exit 0 = pass (teste selado verde E suíte completa sem falha).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SEALED = REPO_ROOT / "judges" / "_sealed" / "j_web" / "index.test.ts"
REGISTRY = REPO_ROOT / "judges" / "registry.tsv"
JUDGE_ID = "j_web"
TARGET_REL = Path("listen-keys") / "index.test.ts"
BNT_REL = Path("node_modules") / ".bin" / "bnt"


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


def parse_bnt_counts(output: str) -> dict:
    """Extrai tests/pass/fail do resumo do `bnt` (node test runner). Usado
    por run_judge.py (futuro) para calcular D3 de forma granular sem
    precisar re-rodar a suíte."""
    total = int(m.group(1)) if (m := re.search(r"ℹ tests (\d+)", output)) else 0
    passed = int(m.group(1)) if (m := re.search(r"ℹ pass (\d+)", output)) else 0
    failed = int(m.group(1)) if (m := re.search(r"ℹ fail (\d+)", output)) else 0
    return {"passed": passed, "failed": failed, "total": total}


def run_bnt(bnt: Path, ws: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [str(bnt), *args],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=100,
        )
    except subprocess.TimeoutExpired:
        print(f"bnt timeout: {args}")
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

    bnt = ws / BNT_REL
    if not bnt.exists():
        print(f"bnt ausente em {bnt} — node_modules/ não provisionado (setup.sh incompleto?)")
        target.unlink(missing_ok=True)
        return 1

    full_counts = {"passed": 0, "failed": 0, "total": 0}
    try:
        target_run = run_bnt(bnt, ws, str(TARGET_REL))
        target_ok = target_run.returncode == 0
        if not target_ok:
            print("teste selado (D1) falhou:")
            print((target_run.stdout + target_run.stderr)[-2000:])

        full_run = run_bnt(bnt, ws)
        full_ok = full_run.returncode == 0
        full_counts = parse_bnt_counts(full_run.stdout + full_run.stderr)
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
