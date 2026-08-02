#!/usr/bin/env python3
"""verify task_j_hw — o teste selado (gabarito do upstream) fica verde.

Copia judges/_sealed/j_hw/tests.c para o workspace SÓ na hora de verificar
(sobrescreve o que o agente tenha deixado em test/tests.c — inclusive se o
agente editou/apagou o arquivo), confere o sha256 contra
judges/registry.tsv, compila e roda `make test_links` (cc + make, sem
hardware real — JSMN_PARENT_LINKS é só uma flag de compilação), e apaga a
cópia selada e o binário compilado do workspace antes de sair.

O gabarito (judges/_sealed/j_hw/tests.c) vem do commit c3131d0 do upstream
zserge/jsmn (não do fix_sha 4ce4404): é o commit que formalizou o teste de
"unmatched brackets" que expõe o bug #81 (fechar `}` sem abrir `{` no nível
raiz não é detectado com JSMN_PARENT_LINKS habilitado). Entre o base_sha e
c3131d0 o único diff em test/tests.c é a função test_unmatched_brackets e a
chamada dela em main() — o resto do arquivo (todas as outras suítes:
test_empty, test_object, test_array, ...) é idêntico ao do base_sha, então
o gabarito compila e roda direto contra o jsmn.c do workspace sem precisar
isolar a função num arquivo à parte.

Só roda `make test_links` (não `make test` completo): as demais variantes
(test_default, test_strict, test_strict_links) já falham no base_sha por um
motivo pré-existente não relacionado ao bug #81 (comportamento de
JSMN_STRICT em "unmatched brackets"), fora do escopo deste juiz.

exit 0 = pass (teste selado verde: PASSED sem FAILED).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SEALED = REPO_ROOT / "judges" / "_sealed" / "j_hw" / "tests.c"
REGISTRY = REPO_ROOT / "judges" / "registry.tsv"
JUDGE_ID = "j_hw"
TARGET_REL = Path("test") / "tests.c"
BINARY_REL = Path("test") / "test_links"


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


def parse_test_counts(output: str) -> dict:
    """Extrai PASSED/FAILED do resumo do runner de teste do jsmn (formato
    próprio, não pytest/tap). Usado por run_judge.py (futuro) para D1/D3
    granular sem precisar re-rodar a suíte."""
    passed = int(m.group(1)) if (m := re.search(r"PASSED:\s*(\d+)", output)) else 0
    failed = int(m.group(1)) if (m := re.search(r"FAILED:\s*(\d+)", output)) else 0
    return {"passed": passed, "failed": failed, "total": passed + failed}


def run_make(ws: Path, target: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["make", target],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print(f"make {target} timeout")
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

    counts = {"passed": 0, "failed": 0, "total": 0}
    try:
        run = run_make(ws, "test_links")
        out = run.stdout + run.stderr
        counts = parse_test_counts(out)
        ok = run.returncode == 0 and counts["failed"] == 0 and counts["passed"] > 0
        if not ok:
            print("teste selado (make test_links) falhou:")
            print(out[-2000:])
    finally:
        # apaga o selado e o binário compilado do workspace — não deve
        # sobrar no ws depois do verify.
        target.unlink(missing_ok=True)
        (ws / BINARY_REL).unlink(missing_ok=True)

    # linha estruturada pro run_judge.py calcular D1/D3 granular sem
    # re-rodar a suíte — o texto acima é só para leitura humana.
    print("JUDGE_RESULT=" + json.dumps({"target_ok": ok, "full": counts}))

    if ok:
        print("ok: teste selado verde (make test_links).")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
