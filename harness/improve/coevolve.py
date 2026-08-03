"""Co-evolução exame×harness: currículo na fronteira da dificuldade (POET).

Um candidato da quarentena só ensina algo se a versão ATUAL do harness FALHA
nele. Este módulo roda os candidatos de `benchmarks/quarantine/` com o mesmo
mecanismo do exame selado (`run_unit`, backend mock por default) e devolve a
fronteira = os que falham hoje. O veredito vai pra `data/frontier.jsonl`.

Fail-closed pro lado da fronteira: erro ao rodar um candidato (exceção, run sem
decisão) NÃO é falha legítima do harness — o candidato é logado e pulado, e não
entra na fronteira. Este módulo NUNCA escreve em `benchmarks/sealed/` nem move
nada: selar continua ato humano (`harness seal`).
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from harness.improve.synthesize import QUARANTINE_DIR
from harness.ledger import store

FRONTIER_NAME = "frontier.jsonl"


def frontier_path() -> Path:
    """`$HARNESS_DATA_DIR/frontier.jsonl`, default `data/frontier.jsonl` relativo
    ao cwd — mesma resolução do ledger (`store.data_dir()`), e em call-time:
    o teste (ou uma run com data dir isolado) muda a env e isto acompanha."""
    return store.data_dir() / FRONTIER_NAME


def _discover(quarantine_dir: Path) -> list[Path]:
    """Mesma convenção do exame: subdirs com `unit.toml`, ordenado."""
    return sorted(p.parent for p in quarantine_dir.glob("*/unit.toml"))


def screen_benchmark(
    unit_dir: Path | str,
    backend: str = "mock",
    model: str = "",
    data_dir: Path | str | None = None,
) -> bool | None:
    """Roda um candidato contra a config atual.

    `True` = o harness passa hoje (exame não ensina nada, fora da fronteira).
    `False` = falha hoje (está na fronteira). `None` = sem veredito (exceção ou
    run sem decisão) — erro não vira fronteira.
    """
    # Import tardio: espelha exam.py e mantém o módulo leve de importar.
    from harness.graph.run_graph import run_unit
    from harness.ledger.store import data_dir as default_data_dir

    unit = Path(unit_dir)
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    # thread_id único: screening nunca retoma run velho por engano.
    thread_id = f"frontier-{unit.name}-{uuid.uuid4().hex[:8]}"
    try:
        state = run_unit(unit, backend, model or None, data, thread_id)
    except Exception as exc:
        print(f"coevolve: '{unit.name}' erro ao rodar, pulado: {exc}", file=sys.stderr)
        return None
    decision = state.get("decision")
    if decision is None:
        print(f"coevolve: '{unit.name}' terminou sem decisão, pulado", file=sys.stderr)
        return None
    return decision.action == "accept"


def _record(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def screen_quarantine(
    backend: str = "mock",
    model: str = "",
    quarantine_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    frontier_path: Path | str | None = None,
    clock: Callable[[], str] | None = None,
) -> list[str]:
    """Nomes dos candidatos que o harness FALHA hoje — a fronteira, ordenada.

    Grava um json por veredito em `data/frontier.jsonl` (append): benchmark,
    passed, timestamp. Candidato pulado (erro) não gera linha nem entra na
    fronteira. Quarentena vazia/inexistente → lista vazia.
    """
    from harness.ledger.store import data_dir as default_data_dir, now_iso

    quarantine = Path(quarantine_dir) if quarantine_dir is not None else QUARANTINE_DIR
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    now = clock or now_iso
    if frontier_path is not None:
        frontier_file = Path(frontier_path)
    else:
        frontier_file = data / FRONTIER_NAME

    if not quarantine.is_dir():
        print(f"coevolve: {quarantine} não existe — fronteira vazia", file=sys.stderr)
        return []

    frontier: list[str] = []
    rows: list[dict] = []
    for unit_dir in _discover(quarantine):
        passed = screen_benchmark(unit_dir, backend, model, data)
        if passed is None:
            continue
        rows.append({"benchmark": unit_dir.name, "passed": passed, "timestamp": now()})
        if not passed:
            frontier.append(unit_dir.name)
    if rows:
        _record(frontier_file, rows)
    return frontier
