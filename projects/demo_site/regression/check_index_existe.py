"""Regression: site/index.html precisa existir e não estar vazio."""

import sys
from pathlib import Path

INDEX = Path("site/index.html")


def main() -> int:
    if not INDEX.exists():
        print("site/index.html não existe")
        return 1

    if INDEX.stat().st_size == 0:
        print("site/index.html está vazio")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
