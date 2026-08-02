"""Nota humana por unidade — o KPI que o harness não sabe medir sozinho.

O harness mede o que sabe medir (testes, peso, erros de HTML). Nada disso diz
se o resultado presta para quem vai usar. A nota humana é esse sinal: 1–5 por
unidade, em `projects/<projeto>/notes.tsv`, append-only (histórico de julgamento
não se edita).

**Nenhuma função de escrita daqui é exposta a backend ou agente**: `add` só é
chamada pelo CLI humano e não entra na allowlist de tools do `ExecRequest`.
Agente que escreve a própria nota transforma o KPI independente em ruído.

Abaixo de `min_notes` na janela, `kpi_value` devolve None ("não medido"): uma
opinião solta não pode reverter uma versão.

Raiz dos projetos: `$HARNESS_PROJECTS_ROOT`, default `projects/` no cwd.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

NOTES_FILE = "notes.tsv"
HEADER = ("ts", "unit_id", "score", "tags", "why")
SCORE_MIN, SCORE_MAX = 1, 5
DEFAULT_WINDOW = 20
MIN_NOTES = 3


def projects_root() -> Path:
    return Path(os.environ.get("HARNESS_PROJECTS_ROOT", "projects"))


def notes_path(project: str) -> Path:
    return projects_root() / project / NOTES_FILE


def load_notes(project: str) -> list[dict[str, str]]:
    """As notas do projeto na ordem do arquivo (mais antiga primeiro)."""
    path = notes_path(project)
    if not path.is_file():
        return []
    lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        cells = (ln.split("\t") + [""] * len(header))[: len(header)]
        rows.append(dict(zip(header, cells)))
    return rows


def add(project: str, unit_id: str, score: int, tags: str = "", why: str = "") -> Path:
    """Append de uma nota; header na 1ª escrita. Score fora da faixa é erro.

    Só o humano chama isto — ver o cabeçalho do módulo.
    """
    if not isinstance(score, int) or not SCORE_MIN <= score <= SCORE_MAX:
        raise ValueError(f"score fora de {SCORE_MIN}..{SCORE_MAX}: {score!r}")
    path = notes_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("\t".join(HEADER) + "\n", encoding="utf-8")
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unit_id": unit_id,
        "score": str(score),
        "tags": tags,
        "why": why,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\t".join(_cell(row[c]) for c in HEADER) + "\n")
    return path


def kpi_value(
    project: str, window: int = DEFAULT_WINDOW, min_notes: int = MIN_NOTES
) -> float | None:
    """Média das últimas `window` notas válidas, ou None com menos de `min_notes`.

    None é "não medido": 0.0 seria confundido com nota ruim.
    """
    scores = _valid_scores(project)[-window:] if window > 0 else []
    if len(scores) < min_notes:
        return None
    return sum(scores) / len(scores)


def _valid_scores(project: str) -> list[float]:
    """Notas parseáveis e dentro da faixa; linha corrompida é descartada."""
    out = []
    for row in load_notes(project):
        try:
            value = float(row.get("score", ""))
        except ValueError:
            continue
        if SCORE_MIN <= value <= SCORE_MAX:
            out.append(value)
    return out


def _cell(value: str) -> str:
    return str(value).replace("\t", " ").replace("\n", " ")
