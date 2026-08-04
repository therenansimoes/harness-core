"""Régua RELATIVA: o piso de qualidade de hoje é a nota de ontem.

Piso absoluto (`--min-nota 6`) tem um platô embutido: assim que a tela chega a 6,
toda run seguinte passa sem melhorar nada, e o loop para de subir com o gate
verde. O baseline é a saída: grava a nota aceita e passa a exigir >= ela.

Um arquivo por workspace, JSON simples, chaves livres (`nota`, `a11y`, ...): quem
grava é quem mede, e o formato não precisa saber quais medidas existirão.
"""

from __future__ import annotations

import json
from pathlib import Path

HARNESS_SUBDIR = ".harness"
BASELINE_FILE = "quality-baseline.json"


def baseline_path(ws: str | Path) -> Path:
    return Path(ws) / HARNESS_SUBDIR / BASELINE_FILE


def save_baseline(ws: str | Path, data: dict) -> Path:
    """Grava o baseline (merge: chave ausente em `data` mantém o valor antigo)."""
    path = baseline_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    atual = load_baseline(ws) or {}
    atual.update({k: v for k, v in data.items() if v is not None})
    path.write_text(json.dumps(atual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_baseline(ws: str | Path) -> dict | None:
    """Baseline gravado, ou None. JSON corrompido é None: piso que não dá para
    ler é piso que não existe, e um gate que explode aqui reprovaria por
    contabilidade, não por qualidade."""
    try:
        obj = json.loads(baseline_path(ws).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["BASELINE_FILE", "baseline_path", "load_baseline", "save_baseline"]
