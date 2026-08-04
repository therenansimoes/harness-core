"""Regression: site/index.html tem tags balanceadas (pilha) e contém <title> e <h1>.

Usa html.parser.HTMLParser da stdlib — sem regex frágil.
"""

import sys
from html.parser import HTMLParser
from pathlib import Path

INDEX = Path("site/index.html")

# Elementos void não têm tag de fechamento (HTML5), então não entram na pilha.
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class StackValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.error: str | None = None
        self.seen_title = False
        self.seen_h1 = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("title",):
            self.seen_title = True
        if tag == "h1":
            self.seen_h1 = True

        if tag in VOID_ELEMENTS:
            return

        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # Tag self-fechada (ex.: <br/>) — não entra na pilha.
        if tag == "title":
            self.seen_title = True
        if tag == "h1":
            self.seen_h1 = True

    def handle_endtag(self, tag: str) -> None:
        if self.error:
            return

        if tag in VOID_ELEMENTS:
            return

        if not self.stack:
            self.error = f"tag de fechamento </{tag}> sem tag aberta correspondente"
            return

        if self.stack[-1] != tag:
            self.error = f"tag </{tag}> fecha fora de ordem, esperava </{self.stack[-1]}>"
            return

        self.stack.pop()


def main() -> int:
    if not INDEX.exists():
        print("site/index.html não existe")
        return 1

    html = INDEX.read_text(encoding="utf-8")

    parser = StackValidator()
    parser.feed(html)
    parser.close()

    if parser.error:
        print(parser.error)
        return 1

    if parser.stack:
        print(f"tag(s) não fechada(s): {', '.join(reversed(parser.stack))}")
        return 1

    if not parser.seen_title:
        print("falta <title> no index")
        return 1

    if not parser.seen_h1:
        print("falta <h1> no index")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
