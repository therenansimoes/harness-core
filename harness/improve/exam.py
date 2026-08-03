"""Exame selado: roda TODAS as unidades de `benchmarks/sealed/*/unit.toml`.

Fail-closed: sealed sem unidades → False (sem exame não se aprova nada);
qualquer exceção → False. `True` só quando toda unidade termina em `accept`.
O loop nunca escreve em `benchmarks/sealed/` — seedar/promover é ato humano.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Mesma convenção cwd-relativa de synthesize.SEALED_DIR.
SEALED_DIR = Path("benchmarks/sealed")


def _discover(sealed_dir: Path) -> list[Path]:
    """Dirs de unidade = subdirs com `unit.toml`. Ordenado = determinístico."""
    return sorted(p.parent for p in sealed_dir.glob("*/unit.toml"))


def exam_report(
    backend: str = "mock",
    model: str = "",
    sealed_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> list[dict]:
    """Uma linha por unidade: {"id": str, "passed": bool} — pro humano ver.

    Exceção numa unidade vira `passed=False` daquela unidade; exceção de
    descoberta sobe (o chamador `run_sealed_exam` traduz em False).
    """
    # Import tardio: espelha run_graph↔cli e mantém o módulo leve de importar.
    from harness.graph.run_graph import run_unit
    from harness.ledger.store import data_dir as default_data_dir

    sealed = Path(sealed_dir) if sealed_dir is not None else SEALED_DIR
    data = Path(data_dir) if data_dir is not None else default_data_dir()

    report: list[dict] = []
    for unit_dir in _discover(sealed):
        unit_id = unit_dir.name
        # thread_id único: exame nunca retoma run velho por engano.
        thread_id = f"exam-{unit_id}-{uuid.uuid4().hex[:8]}"
        try:
            state = run_unit(unit_dir, backend, model or None, data, thread_id)
            decision = state.get("decision")
            passed = bool(decision is not None and decision.action == "accept")
        except Exception:
            passed = False
        report.append({"id": unit_id, "passed": passed})
    return report


def run_sealed_exam(
    backend: str = "mock",
    model: str = "",
    sealed_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> bool:
    """True SÓ se todas as unidades seladas aceitam. Vazio ou erro → False."""
    try:
        report = exam_report(
            backend=backend, model=model, sealed_dir=sealed_dir, data_dir=data_dir
        )
    except Exception as exc:
        print(f"exam: erro no exame selado, reprovando (fail-closed): {exc}",
              file=sys.stderr)
        return False
    if not report:
        print("exam: benchmarks/sealed sem unidades — fail-closed, nada aprovado",
              file=sys.stderr)
        return False
    return all(r["passed"] for r in report)
