"""Acceptance s001: site/precos.html tem exatamente 3 blocos class="plano" e um <h1>."""
import sys
from html.parser import HTMLParser
from pathlib import Path

PRECOS = Path("site/precos.html")


class PlanosCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.planos = 0
        self.h1_count = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._check(tag, attrs)

    def _check(self, tag: str, attrs) -> None:
        if tag == "h1":
            self.h1_count += 1

        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "").split()
        if "plano" in classes:
            self.planos += 1


def main() -> int:
    if not PRECOS.exists():
        print("site/precos.html não existe")
        return 1

    html = PRECOS.read_text(encoding="utf-8")

    parser = PlanosCounter()
    parser.feed(html)
    parser.close()

    if parser.planos != 3:
        print(f"esperava 3 blocos class=\"plano\", encontrou {parser.planos}")
        return 1

    if parser.h1_count != 1:
        print(f"esperava 1 <h1>, encontrou {parser.h1_count}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
