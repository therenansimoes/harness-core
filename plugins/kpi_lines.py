"""Plugin seed da zona de código mutável: KPIs de linhas por arquivo."""

from __future__ import annotations

from pathlib import Path


def collect(path: Path | str) -> dict:
    """Contagem de arquivos .py e linhas totais sob `path`."""
    base = Path(path)
    files = [p for p in sorted(base.rglob("*.py")) if p.is_file()]
    return {
        "files": len(files),
        "lines": sum(
            len(p.read_text(encoding="utf-8").splitlines()) for p in files
        ),
    }
