#!/usr/bin/env python3
"""verify task_s02 — wc_lite.py trata argv e códigos de saída corretamente.

exit 0 = pass.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = "wc_lite.py"
USO = "ERRO: uso: wc_lite.py <arquivo>"

if not Path(SCRIPT).exists():
    print(f"{SCRIPT} não existe")
    sys.exit(1)


def run(args, timeout=15):
    try:
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"{SCRIPT} {' '.join(args)}: timeout")
        sys.exit(1)


fails = []

# caso 1a: zero argumentos
proc = run([])
if proc.returncode != 2:
    fails.append(f"0 args: exit esperado 2, obtido {proc.returncode}")
if proc.stdout.strip() != "":
    fails.append("0 args: stdout deveria estar vazio")
if proc.stderr.strip() != USO:
    fails.append(f"0 args: stderr esperado {USO!r} obtido {proc.stderr.strip()!r}")

# caso 1b: dois argumentos
proc = run(["a.txt", "b.txt"])
if proc.returncode != 2:
    fails.append(f"2 args: exit esperado 2, obtido {proc.returncode}")
if proc.stderr.strip() != USO:
    fails.append(f"2 args: stderr esperado {USO!r} obtido {proc.stderr.strip()!r}")

# caso 2: arquivo inexistente
caminho_inexistente = "__nao_existe_verify_s02__.txt"
if Path(caminho_inexistente).exists():
    print("ambiente inconsistente: arquivo de teste 'inexistente' já existe")
    sys.exit(1)
proc = run([caminho_inexistente])
esperado = f"ERRO: arquivo nao encontrado: {caminho_inexistente}"
if proc.returncode != 3:
    fails.append(f"arquivo inexistente: exit esperado 3, obtido {proc.returncode}")
if proc.stdout.strip() != "":
    fails.append("arquivo inexistente: stdout deveria estar vazio")
if proc.stderr.strip() != esperado:
    fails.append(
        f"arquivo inexistente: stderr esperado {esperado!r} obtido {proc.stderr.strip()!r}"
    )

# caso 3: arquivo vazio
vazio = Path("__verify_s02_vazio__.txt")
vazio.write_text("", encoding="utf-8")
proc = run([str(vazio)])
if proc.returncode != 4:
    fails.append(f"arquivo vazio: exit esperado 4, obtido {proc.returncode}")
if proc.stdout.strip() != "":
    fails.append("arquivo vazio: stdout deveria estar vazio")
if proc.stderr.strip() != "ERRO: arquivo vazio":
    fails.append(
        f"arquivo vazio: stderr esperado 'ERRO: arquivo vazio' obtido {proc.stderr.strip()!r}"
    )
vazio.unlink(missing_ok=True)

# caso 4: arquivo válido com conteúdo conhecido
texto = "primeira linha aqui\nsegunda linha com mais palavras\n\nquarta linha\n"
valido = Path("__verify_s02_valido__.txt")
valido.write_text(texto, encoding="utf-8")
golden = "\n".join(
    [
        f"LINHAS: {len(texto.splitlines())}",
        f"PALAVRAS: {len(texto.split())}",
        f"CARACTERES: {len(texto)}",
    ]
)
proc = run([str(valido)])
if proc.returncode != 0:
    fails.append(f"arquivo válido: exit esperado 0, obtido {proc.returncode}")
if proc.stderr.strip() != "":
    fails.append("arquivo válido: stderr deveria estar vazio")
if proc.stdout.strip("\n") != golden:
    fails.append(
        f"arquivo válido: stdout esperado {golden!r} obtido {proc.stdout.strip(chr(10))!r}"
    )
valido.unlink(missing_ok=True)

if fails:
    print(" | ".join(fails))
    sys.exit(1)
print("ok")
