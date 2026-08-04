"""Co-evolução exame×harness: currículo na fronteira da dificuldade (POET).

Um candidato da quarentena só ensina algo se a versão ATUAL do harness FALHA
nele. Este módulo roda os candidatos de `benchmarks/quarantine/` com o mesmo
mecanismo do exame selado (`run_unit`, executor de `[frontier]`/`[exam]` no
`config/ruler.toml`) e devolve a fronteira = os que falham hoje. O veredito vai
pra `data/frontier.jsonl`.

Fail-closed pro lado da fronteira: erro ao rodar um candidato (exceção, run sem
decisão) NÃO é falha legítima do harness — o candidato é logado e pulado, e não
entra na fronteira. Este módulo NUNCA escreve em `benchmarks/sealed/` nem move
nada: selar continua ato humano (`harness seal`).
"""

from __future__ import annotations

import json
import sys
import time
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
    backend: str | None = None,
    model: str | None = None,
    data_dir: Path | str | None = None,
) -> bool | None:
    """Roda um candidato contra a config atual.

    `True` = o harness passa hoje (exame não ensina nada, fora da fronteira).
    `False` = falha hoje (está na fronteira). `None` = sem veredito (exceção,
    run sem decisão, ou unidade pulada) — erro não vira fronteira.

    `backend`/`model` explícitos vencem o config; `None` lê `[frontier]` (com
    fallback `[exam]`) do `config/ruler.toml`.
    """
    # Import tardio: espelha exam.py e mantém o módulo leve de importar.
    from harness.graph.run_graph import run_unit
    from harness.improve.exam import MOCK_BACKEND, _requires_real_backend, frontier_backend
    from harness.ledger.store import data_dir as default_data_dir
    from harness.memory import episodic

    if backend is None or model is None:
        cfg_backend, cfg_model = frontier_backend()
        backend = backend or cfg_backend
        model = model or cfg_model

    unit = Path(unit_dir)
    # Paridade com o exame: unidade que só um executor de verdade resolve não
    # é screenada pelo mock. Sem isto ela "falha" sempre e entra na fronteira
    # por artefato do mock, não por dificuldade.
    if backend == MOCK_BACKEND and _requires_real_backend(unit):
        print(
            f"coevolve: '{unit.name}' exige backend real, fora do screening mock", file=sys.stderr
        )
        return None
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    # thread_id único: screening nunca retoma run velho por engano.
    thread_id = f"frontier-{unit.name}-{uuid.uuid4().hex[:8]}"
    # Screening não grava nem lê memória episódica — mesma classe do exame: o
    # verificador do candidato imprime o gabarito ("esperado=...") e a memória
    # global vazaria isso pro executor. `screen_quarantine` passa por aqui.
    with episodic.disabled():
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
    backend: str | None = None,
    model: str | None = None,
    quarantine_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    frontier_path: Path | str | None = None,
    clock: Callable[[], str] | None = None,
    max_units: int = 0,
    deadline_s: float = 0.0,
) -> list[str]:
    """Nomes dos candidatos que o harness FALHA hoje — a fronteira, ordenada.

    Grava um json por veredito em `data/frontier.jsonl` (append): benchmark,
    passed, timestamp, backend, model, real, sec. Candidato pulado (erro, ou
    unidade que exige backend real sob o mock) não gera linha nem entra na
    fronteira. Quarentena vazia/inexistente → lista vazia.

    `max_units`/`deadline_s` (0 = sem teto) limitam o screening: com backend
    real cada candidato custa tempo e dinheiro. Teto batido para o laço e avisa
    no stderr — o que NÃO rodou não entra na fronteira (fail-closed pro lado da
    fronteira: "não sei" nunca vira "está na fronteira").
    """
    from harness.improve.exam import MOCK_BACKEND
    from harness.ledger.store import data_dir as default_data_dir
    from harness.ledger.store import now_iso

    if backend is None or model is None:
        from harness.improve.exam import frontier_backend

        cfg_backend, cfg_model = frontier_backend()
        backend = backend or cfg_backend
        model = model or cfg_model

    quarantine = Path(quarantine_dir) if quarantine_dir is not None else QUARANTINE_DIR
    data = Path(data_dir) if data_dir is not None else default_data_dir()
    now = clock or now_iso
    frontier_file = Path(frontier_path) if frontier_path is not None else data / FRONTIER_NAME

    if not quarantine.is_dir():
        print(f"coevolve: {quarantine} não existe — fronteira vazia", file=sys.stderr)
        return []

    real = backend != MOCK_BACKEND
    started = time.monotonic()
    frontier: list[str] = []
    rows: list[dict] = []
    units = _discover(quarantine)
    for i, unit_dir in enumerate(units):
        if max_units > 0 and i >= max_units:
            print(
                f"coevolve: teto de {max_units} unidades batido — "
                f"{len(units) - i} candidato(s) não screenado(s)",
                file=sys.stderr,
            )
            break
        elapsed = time.monotonic() - started
        if deadline_s > 0.0 and elapsed >= deadline_s:
            print(
                f"coevolve: deadline de {deadline_s}s batido ({elapsed:.1f}s) — "
                f"{len(units) - i} candidato(s) não screenado(s)",
                file=sys.stderr,
            )
            break
        unit_started = time.monotonic()
        passed = screen_benchmark(unit_dir, backend, model, data)
        if passed is None:
            continue
        rows.append(
            {
                "benchmark": unit_dir.name,
                "passed": passed,
                "timestamp": now(),
                "backend": backend,
                "model": model,
                "real": real,
                "sec": round(time.monotonic() - unit_started, 3),
            }
        )
        if not passed:
            frontier.append(unit_dir.name)
    if rows:
        _record(frontier_file, rows)
    return frontier
