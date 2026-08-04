#!/usr/bin/env python3
"""Concatena os fragmentos de prompts/tools.d/ em prompts/tools.md.

Fonte da verdade = prompts/tools.d/*.md, lidos em ordem alfabética de nome.
O arquivo gerado leva um header de aviso e é idempotente: rodar duas vezes
seguidas produz exatamente os mesmos bytes.
"""

from __future__ import annotations

import sys
from pathlib import Path

HEADER = "<!-- GERADO por scripts/build_prompts.py — edite prompts/tools.d/ -->\n"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_DIR = REPO_ROOT / "prompts" / "tools.d"
DEFAULT_OUT = REPO_ROOT / "prompts" / "tools.md"


def render(src_dir: Path) -> str:
    """Devolve o conteúdo final (header + fragmentos em ordem alfabética)."""
    fragments = sorted(src_dir.glob("*.md"), key=lambda p: p.name)
    if not fragments:
        raise SystemExit(f"nenhum fragmento *.md em {src_dir}")
    body = "".join(f.read_text(encoding="utf-8") for f in fragments)
    return HEADER + body


def build(src_dir: Path = DEFAULT_SRC_DIR, out: Path = DEFAULT_OUT) -> str:
    """Escreve `out` a partir de `src_dir`. Não reescreve se já está igual."""
    content = render(src_dir)
    if out.exists() and out.read_text(encoding="utf-8") == content:
        return content
    out.write_text(content, encoding="utf-8")
    return content


def main(argv: list[str]) -> int:
    src_dir = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_SRC_DIR
    out = Path(argv[2]).resolve() if len(argv) > 2 else DEFAULT_OUT
    build(src_dir, out)
    print(f"gerado {out} a partir de {src_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
