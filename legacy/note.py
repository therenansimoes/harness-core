#!/usr/bin/env python3
"""note.py — nota humana por task, como KPI de projeto (D8.1).

O harness mede o que sabe medir (testes, peso, erros de HTML). Nada disso diz
se o resultado presta para quem vai usar. A nota humana é esse sinal: 0–5 por
task, gravada em `projects/<projeto>/notes.tsv` (append-only), lida de volta
como um KPI comum via `[kpi.human_score]` no kpi.toml do alvo.

Duas regras que existem para a nota não virar sinal falso:

- `add` recusa task_id que não está no results.tsv do projeto. Nota fantasma
  (typo no id, task que nunca rodou) mediria uma run que não existiu.
- `kpi` só imprime número com pelo menos MIN_NOTES notas na janela; abaixo
  disso sai vazio com exit 1, que o kpi.py lê como `nan` ("não medido").
  score.KPI_MIN_N já exige 3 valores por lado no A/B — deixar 1 nota isolada
  virar média daria a uma opinião solta o poder de reverter uma versão.

CLI:
    python3 note.py add <projeto> <task_id> --score 0..5 [--tag a,b] [--why "…"]
    python3 note.py kpi <projeto> [--window 6]
    python3 note.py list <projeto> [-n 10]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

NOTES_HEADER = ["ts", "task_id", "score", "tags", "why", "author"]
SCORE_MIN, SCORE_MAX = 0, 5
DEFAULT_WINDOW = 6
MIN_NOTES = 3  # espelha score.KPI_MIN_N: abaixo disso não há veredito


def _projects_root() -> Path:
    """Relido a cada chamada: os testes trocam HARNESS_PROJECTS_ROOT depois do
    import (project.py resolve no import porque o CLI dele é um processo só)."""
    return Path(os.environ.get("HARNESS_PROJECTS_ROOT", ROOT / "projects"))


def notes_path(project: str) -> Path:
    return _projects_root() / project / "notes.tsv"


def _cell(v) -> str:
    return str(v).replace("\t", " ").replace("\n", " ")


def load_notes(project: str) -> list[dict]:
    """As notas do projeto na ordem do arquivo (mais antiga primeiro)."""
    path = notes_path(project)
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        cells = (ln.split("\t") + [""] * len(header))[: len(header)]
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


def known_task_ids(project: str) -> list[str]:
    """task_id do results.tsv do projeto, na ordem do arquivo."""
    path = _projects_root() / project / "results.tsv"
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    if "task_id" not in header:
        return []
    i = header.index("task_id")
    out = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        if len(cells) > i and cells[i].strip():
            out.append(cells[i].strip())
    return out


def resolve_task_id(project: str, task_id: str) -> str | None:
    """O id completo ("projeto/0004") a partir do completo ou só da unit
    ("0004"). None = não existe no results.tsv — a nota seria fantasma."""
    known = known_task_ids(project)
    if task_id in known:
        return task_id
    for full in known:
        if full.rsplit("/", 1)[-1] == task_id:
            return full
    return None


def add(project: str, task_id: str, score: int, tags: str, why: str) -> Path:
    """Append de uma nota. Header na 1ª escrita; nunca reescreve linha antiga —
    histórico de julgamento não se edita."""
    path = notes_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("\t".join(NOTES_HEADER) + "\n")
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "task_id": task_id,
        "score": score,
        "tags": tags,
        "why": why,
        "author": os.environ.get("USER", "human"),
    }
    with path.open("a") as fh:
        fh.write("\t".join(_cell(row[c]) for c in NOTES_HEADER) + "\n")
    return path


def _valid_scores(project: str) -> list[float]:
    out = []
    for r in load_notes(project):
        try:
            v = float(r.get("score", ""))
        except ValueError:
            continue
        if SCORE_MIN <= v <= SCORE_MAX:
            out.append(v)
    return out


def kpi_value(project: str, window: int = DEFAULT_WINDOW) -> float:
    """Média das últimas `window` notas válidas. Sem notas => 0.0 não serve
    (0 é nota legítima) — quem chama tem que checar a contagem antes."""
    xs = _valid_scores(project)[-window:]
    if not xs:
        return float("nan")
    return sum(xs) / len(xs)


# ------------------------------------------------------------------ comandos


def cmd_add(a: argparse.Namespace) -> int:
    if not (SCORE_MIN <= a.score <= SCORE_MAX):
        print(f"note: --score fora de {SCORE_MIN}..{SCORE_MAX}: {a.score}", file=sys.stderr)
        return 2
    full = resolve_task_id(a.project, a.task_id)
    if full is None:
        known = known_task_ids(a.project)
        print(
            f"note: task_id {a.task_id!r} não está no results.tsv de {a.project}", file=sys.stderr
        )
        if known:
            print("últimos ids: " + ", ".join(known[-5:]), file=sys.stderr)
        else:
            print("(nenhuma run registrada nesse projeto)", file=sys.stderr)
        return 2
    path = add(a.project, full, a.score, a.tag or "", a.why or "")
    print(f"note: {full} score={a.score} -> {path}")
    return 0


def cmd_kpi(a: argparse.Namespace) -> int:
    xs = _valid_scores(a.project)[-a.window :]
    if len(xs) < MIN_NOTES:
        print(
            f"note: {len(xs)} nota(s) na janela de {a.window}, mínimo {MIN_NOTES} — sem veredito",
            file=sys.stderr,
        )
        return 1
    print(sum(xs) / len(xs))
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    rows = load_notes(a.project)[-a.n :]
    for r in rows:
        print("\t".join(_cell(r.get(c, "")) for c in NOTES_HEADER))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="note.py", description="nota humana por task")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="grava uma nota")
    a.add_argument("project")
    a.add_argument("task_id")
    a.add_argument("--score", type=int, required=True, help=f"{SCORE_MIN}..{SCORE_MAX}")
    a.add_argument("--tag", default="", help="tags separadas por vírgula")
    a.add_argument("--why", default="", help="por que essa nota")
    a.set_defaults(func=cmd_add)

    k = sub.add_parser("kpi", help="média das últimas notas (contrato de kpi.py)")
    k.add_argument("project")
    k.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    k.set_defaults(func=cmd_kpi)

    s = sub.add_parser("list", help="últimas notas")
    s.add_argument("project")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(func=cmd_list)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
