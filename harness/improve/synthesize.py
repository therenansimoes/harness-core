"""Falhas do harness viram exames novos — em quarentena, nunca direto em sealed.

Cada unidade que falhou (ou teve mutação revertida) vira um dir em
`benchmarks/quarantine/<slug>/` com o `unit.toml` da unidade original mais a
origem (run_id, exit_reason). Promover para `benchmarks/sealed/` é ato humano:
`harness seal <name> --yes`. Este módulo NUNCA escreve em sealed.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

from harness.types import RunRow

QUARANTINE_DIR = Path("benchmarks/quarantine")
SEALED_DIR = Path("benchmarks/sealed")
DEFAULT_UNITS_DIR = Path("benchmarks/held_in")
REVERTED = "reverted"


def _slug(unit_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", unit_id.lower()).strip("-") or "unit"


def _s(value: str) -> str:
    # json.dumps produz uma basic string válida em TOML (escapes compatíveis)
    return json.dumps(value)


def _failed(row: RunRow) -> bool:
    return (not row.ok) or row.exit_reason == REVERTED


def synthesize_from_failures(
    history: Iterable[RunRow],
    out_dir: Path = QUARANTINE_DIR,
    units_dir: Path = DEFAULT_UNITS_DIR,
) -> list[Path]:
    """Gera exames de quarentena a partir das runs falhas/revertidas.

    Dedupe por unit id: unidade já vista no batch ou já presente em `out_dir`
    não é regenerada. Unidade sem `unit.toml` original em `units_dir` é pulada
    — sem prompt/verify_cmd reais não há exame honesto para sintetizar.
    Devolve os paths dos diretórios criados.
    """
    created: list[Path] = []
    seen: set[str] = set()
    for row in history:
        if not _failed(row) or row.unit_id in seen:
            continue
        seen.add(row.unit_id)
        dst = out_dir / _slug(row.unit_id)
        if (dst / "unit.toml").exists():
            continue
        src = units_dir / row.unit_id / "unit.toml"
        if not src.is_file():
            continue
        data = tomllib.loads(src.read_text(encoding="utf-8"))
        lines = [
            "# Sintetizado de uma falha do harness — quarentena, não selado.",
            f"# Origem: run {row.run_id} ({row.exit_reason}).",
            f"id = {_s(str(data['id']))}",
        ]
        if data.get("kind") is not None:
            lines.append(f"kind = {_s(str(data['kind']))}")
        lines += [
            f"prompt = {_s(str(data['prompt']))}",
            f"verify_cmd = {_s(str(data['verify_cmd']))}",
            "",
            "[origin]",
            f"run_id = {_s(row.run_id)}",
            f"exit_reason = {_s(row.exit_reason)}",
        ]
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "unit.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        created.append(dst)
    return created
