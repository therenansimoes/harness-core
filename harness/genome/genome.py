"""Genoma: dois grupos de padrões e a regra de quem pode tocar o quê.

`immutable` é a blocklist — régua, roteador, topologia do grafo, exames
selados e o lock de deps. `mutable` é o que o loop pode calibrar. Um padrão
literalmente repetido nos dois grupos é contradição de configuração, não
ambiguidade: `load()` recusa carregar. A colisão que só aparece em runtime
(padrões diferentes cobrindo o mesmo path) vira violação em `check_patch` —
falhar fechado em vez de inventar precedência no silêncio.

Matching é glob estilo pathlib sobre o path relativo à raiz do repo, em
posix: `*`, `?` e `[...]` ficam dentro de um segmento; o segmento `**` casa
um ou mais segmentos.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

DEFAULT_PATH = Path("config/genome.toml")

IMMUTABLE = "genome:immutable"
CONFLICT = "genome:conflict"
ESCAPE = "genome:escape"

_WILDCARDS = "*?["


@dataclass(frozen=True)
class Genome:
    """Os padrões já normalizados, na ordem do toml."""

    immutable: tuple[str, ...]
    mutable: tuple[str, ...] = ()


def load(path: Path = DEFAULT_PATH) -> Genome:
    """Lê o `genome.toml`. Fail-closed: sem arquivo não existe genoma."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"genome não existe: {p}")
    data = tomllib.loads(p.read_text(encoding="utf-8"))

    groups: dict[str, tuple[str, ...]] = {}
    for key in ("immutable", "mutable"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{p}: '{key}' precisa ser uma lista de strings")
        groups[key] = tuple(_norm(x) for x in value)
    if not groups["immutable"]:
        raise ValueError(f"{p}: 'immutable' vazia — a blocklist é o ponto do arquivo")

    dup = sorted(set(groups["immutable"]) & set(groups["mutable"]))
    if dup:
        raise ValueError(f"{p}: padrão nos dois grupos: {', '.join(dup)}")
    return Genome(immutable=groups["immutable"], mutable=groups["mutable"])


def check_patch(g: Genome, changed: Iterable[str], root: Path | None = None) -> list[str]:
    """Violações do patch, na ordem em que os paths aparecem.

    Casa `immutable` → `genome:immutable:<path>`; casa os dois grupos →
    `genome:conflict:<path>`; escapa a raiz → `genome:escape:<path>`. Path
    fora dos dois grupos é código comum — trabalho normal, não violação.
    Com `root`, um path é resolvido (realpath) antes de comparar: symlink
    apontando pra fora é escape, não path relativo inocente.
    """
    out: list[str] = []
    for raw in changed:
        rel = _relative(raw, root)
        if rel is None:
            out.append(f"{ESCAPE}:{_norm(str(raw))}")
        elif matches(rel, g.immutable):
            kind = CONFLICT if matches(rel, g.mutable) else IMMUTABLE
            out.append(f"{kind}:{rel}")
    # o mesmo path duas vezes no diff não são duas violações
    return list(dict.fromkeys(out))


def violation_path(violation: str) -> str:
    """`<prefixo>:<motivo>:<path>` -> `<path>` (o path pode conter ':')."""
    return violation.partition(":")[2].partition(":")[2]


def matches(rel: str, patterns: Iterable[str]) -> bool:
    return any(_compiled(pat).match(rel) for pat in patterns)


def search_root(pattern: str) -> str:
    """Prefixo literal do padrão: por onde começar a varrer a árvore."""
    parts: list[str] = []
    for seg in pattern.split("/"):
        if any(ch in seg for ch in _WILDCARDS):
            break
        parts.append(seg)
    return "/".join(parts)


def _norm(pattern: str) -> str:
    """Forma canônica de comparação: posix, relativo, sem './'."""
    return PurePosixPath(pattern.replace("\\", "/")).as_posix().removeprefix("./")


def _relative(raw: object, root: Path | None) -> str | None:
    """Path relativo à raiz em posix, ou None se escapa dela.

    Resolve symlink com realpath, e não abspath, pelo motivo do `guard_path`
    do legado: abspath não enxerga o link que aponta pra fora do root.
    """
    s = str(raw).replace("\\", "/")
    if root is not None:
        base = os.path.realpath(root)
        target = os.path.realpath(os.path.join(base, s))
        if target != base and not target.startswith(base + os.sep):
            return None
        s = os.path.relpath(target, base).replace(os.sep, "/")

    p = PurePosixPath(s)
    if p.is_absolute():
        return None
    parts: list[str] = []
    for part in p.parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    """Glob -> regex. `**` atravessa `/`, o resto fica no segmento."""
    segments = pattern.split("/")
    out: list[str] = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "**":
            out.append("[^/]+(?:/[^/]+)*" if last else "(?:[^/]+/)*")
            continue
        out.append(_segment(seg))
        if not last:
            out.append("/")
    return re.compile("".join(out) + r"\Z")


def _segment(seg: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
            while i < len(seg) and seg[i] == "*":  # '**' colado a texto é só '*'
                i += 1
            continue
        if c == "[":
            end = _class_end(seg, i)
            if end is not None:
                body = seg[i + 1 : end].replace("\\", "\\\\")
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = end + 1
                continue
            out.append(re.escape(c))  # '[' sem fechar é literal
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _class_end(seg: str, start: int) -> int | None:
    j = start + 1
    if j < len(seg) and seg[j] == "!":
        j += 1
    if j < len(seg) and seg[j] == "]":  # ']' logo no início da classe é literal
        j += 1
    while j < len(seg) and seg[j] != "]":
        j += 1
    return j if j < len(seg) else None
