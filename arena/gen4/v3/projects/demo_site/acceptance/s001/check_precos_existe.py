"""Acceptance s001: site/precos.html precisa existir."""
import sys
from pathlib import Path

PRECOS = Path("site/precos.html")


def main() -> int:
    if not PRECOS.exists():
        print("site/precos.html não existe")
        return 1

    if PRECOS.stat().st_size == 0:
        print("site/precos.html está vazio")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
