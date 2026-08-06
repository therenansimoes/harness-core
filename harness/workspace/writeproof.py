"""Prova-de-escrita contra o baseline do provision, não contra `git status` cru.

`git status --porcelain` na worktree isolada enxerga o lixo do PRÓPRIO harness:
`ws_setup.ensure` grava `.harness/setup.log` e `env_file` ANTES do agente rodar,
e ferramentas do agente ainda somam `symbols.json`/`repomap.json`/`procs.json`.
Uma régua que conta esse lixo como "o agente escreveu" aceita run onde o
agente não tocou em nada — o `unit.toml` do repo fonte não é o que a régua vê
(a worktree só tem o que está trackeado no HEAD), então o vácuo do `.harness/`
sempre casa.

A defesa é gravar, no instante do provision, o que já estava sujo — e comparar
contra isso, não contra zero. `new_paths` só some quando o baseline existe e é
legível; ausência de prova é REPROVAÇÃO, nunca aceite silencioso.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from harness.workspace import sealing

SCRATCH = ".harness"
# Definição ÚNICA de "não conta como trabalho do agente": scratch do harness,
# caches de dependência symlinkados pelo setup e os artefatos de régua que o
# grafo grava no workspace. `projects.deliver` usa a MESMA tupla para excluir
# do commit de entrega — as duas réguas (prova-de-escrita e "o que entrega")
# concordam por construção, não por sorte de manter duas listas em sincronia.
NOT_A_WRITE: tuple[str, ...] = (
    ".harness",
    "node_modules",
    ".venv",
    ".cache",
    "verify.log",
    "trace.jsonl",
)
BASELINE_REL = Path(".harness") / "write-baseline.json"


def dirty(ws: Path) -> tuple[str, ...]:
    """Paths sujos na worktree, excluindo `NOT_A_WRITE` e o verificador selado.

    `sealing.is_verifier` cai fora porque `verify.py` é materializado na ws
    ENQUANTO a régua roda (`sealing.verifier_visible`) — contar sua presença
    como prova de trabalho deixaria a régua provar a si mesma. Git com exit
    != 0 (não-repo, path quebrado) levanta: quem chama decide o fail-closed.
    """
    ws = Path(ws)
    excludes = [f":(exclude){nome}" for nome in NOT_A_WRITE]
    proc = subprocess.run(
        ["git", "-C", str(ws), "status", "--porcelain", "-z", "--", *excludes],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"writeproof: git status falhou (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    campos = iter(c for c in proc.stdout.split("\0") if c)
    paths: list[str] = []
    for entrada in campos:
        status, rel = entrada[:2], entrada[3:]
        if status[0] in ("R", "C"):
            next(campos, None)  # consome o path de origem do rename/copy
        if sealing.is_verifier(rel):
            continue
        paths.append(rel)
    return tuple(paths)


def save_baseline(ws: Path) -> None:
    """Grava o snapshot do provision em `ws/BASELINE_REL`.

    Sem try/except aqui de propósito: se `dirty` levantar (não-repo) ou a
    escrita falhar (OSError), a exceção sobe — quem chama decide engolir, e
    engolir NUNCA pode deixar um arquivo pela metade fingindo sucesso. Sem
    arquivo, `new_paths` devolve None e a régua downstream REPROVA.
    """
    ws = Path(ws)
    payload = json.dumps({"paths": sorted(dirty(ws)), "ts": time.time()})
    alvo = ws / BASELINE_REL
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_text(payload, encoding="utf-8")


def new_paths(ws: Path) -> tuple[str, ...] | None:
    """Paths sujos desde o baseline, ou None sem baseline usável.

    None cobre baseline ausente, ilegível ou workspace fora de um repo git —
    as três são "sem garantia", e a régua trata todas como REPROVAÇÃO. Tupla
    vazia é caso distinto: baseline presente, nada novo — o agente não
    escreveu nada, e isso também reprova, só que por motivo diferente.
    """
    ws = Path(ws)
    try:
        raw = json.loads((ws / BASELINE_REL).read_text(encoding="utf-8"))
        baseline = raw["paths"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    try:
        atual = dirty(ws)
    except RuntimeError:
        return None
    return tuple(sorted(set(atual) - set(baseline)))
