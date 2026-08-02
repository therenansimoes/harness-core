#!/usr/bin/env python3
"""accept.py — B1 (SPEC-J2 design 2, trilha B) do build_j_b2b.

Copia judges/_sealed/build_j_b2b/test_pricing.py pro workspace SÓ na hora
de verificar (sobrescreve o que o agente tenha deixado, inclusive se ele
editou/apagou o arquivo de teste — mesmo tamper-check de D2 na J1),
confere o sha256 contra judges/registry_build.tsv, roda `python3 -m
unittest` contra o `pricing.py` do workspace e apaga a cópia selada antes
de sair.

`registry_build.tsv` é separado do `judges/registry.tsv` da J1 de
propósito: esse último é enumerado por `run_judge.py`/`test_judges.py`
como "os 3 juízes task_*" (base_sha sha1 de 40 chars, dry-run por
judge_id) — misturar a trilha B ali sem tocar nesses arquivos (fora do
escopo desta entrega) quebra esse contrato. Wire-up de verdade em
`run_judge.py`/`evolve.py`/`graph.py` é trabalho de outro fluxo (ver
SPEC-J2 design 2, aceite 5/6).

Workspace = diretório passado como argv[1], ou `seed/` (irmão deste
arquivo) por default — rodar `python3 accept.py` sem argumento verifica o
seed intocado, que é vermelho por construção (nada foi implementado ainda).

exit 0 = pass (suíte selada 100% verde). exit != 0 = fail.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SEALED = REPO_ROOT / "judges" / "_sealed" / "build_j_b2b" / "test_pricing.py"
REGISTRY = REPO_ROOT / "judges" / "registry_build.tsv"
JUDGE_ID = "build_j_b2b"
TARGET_NAME = "test_pricing.py"


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


def resolve_workspace() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    return (Path(__file__).resolve().parent / "seed").resolve()


def main() -> int:
    if not SEALED.exists():
        print(f"arquivo selado ausente: {SEALED}")
        return 1

    expected_sha = registry_sealed_sha256()
    actual_sha = hashlib.sha256(SEALED.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        print(f"sealed_sha256 não bate: registry={expected_sha} arquivo={actual_sha}")
        return 1

    ws = resolve_workspace()
    if not (ws / "pricing.py").exists():
        print(f"pricing.py ausente em {ws}")
        return 1

    target = ws / TARGET_NAME
    target.write_bytes(SEALED.read_bytes())
    try:
        run = subprocess.run(
            [sys.executable, "-m", "unittest", "test_pricing", "-v"],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(run.stdout)
        print(run.stderr)
        ok = run.returncode == 0
    finally:
        # apaga o selado do workspace — não deve sobrar lá depois do accept.
        target.unlink(missing_ok=True)

    if ok:
        print("ok: suíte selada de pricing.py verde.")
        return 0
    print("falhou: suíte selada de pricing.py com erro/falha.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
