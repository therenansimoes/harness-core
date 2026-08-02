"""Regression: index.html precisa linkar style.css, e o arquivo tem que existir."""
import sys
from html.parser import HTMLParser
from pathlib import Path

INDEX = Path("site/index.html")
CSS = Path("site/style.css")


class LinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        d = dict(attrs)
        rel = (d.get("rel") or "").lower()
        if "stylesheet" in rel and d.get("href"):
            self.hrefs.append(d["href"])


def main() -> int:
    if not INDEX.exists():
        print("site/index.html não existe")
        return 1

    p = LinkFinder()
    p.feed(INDEX.read_text(encoding="utf-8", errors="replace"))
    if not any("style.css" in h for h in p.hrefs):
        print(f"index.html não linka style.css (stylesheets encontrados: {p.hrefs})")
        return 1

    if not CSS.exists():
        print("site/style.css é referenciado mas não existe no disco")
        return 1

    if CSS.stat().st_size == 0:
        print("site/style.css está vazio")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
