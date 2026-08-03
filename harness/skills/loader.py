"""Skills: guidance em markdown + frontmatter TOML, injetada no system prompt.

Formato compartilhado: primeira linha '---', linhas TOML (name/kinds/description),
'---', corpo markdown. Arquivo malformado é pulado — loader nunca levanta.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path("skills")


@dataclass(frozen=True)
class Skill:
    name: str
    kinds: tuple[str, ...]
    description: str
    body: str
    path: Path


def load_skills(root: Path = DEFAULT_ROOT) -> list[Skill]:
    """Dir ausente => []. Arquivo malformado => pulado."""
    if not root.is_dir():
        return []
    out: list[Skill] = []
    for path in sorted(root.glob("*.md")):
        skill = _parse(path)
        if skill is not None:
            out.append(skill)
    return out


def select_skills(kind: str | None, root: Path = DEFAULT_ROOT) -> list[Skill]:
    """`kinds` vazio = vale para todo kind; kind None só casa com esses."""
    return [s for s in load_skills(root) if not s.kinds or kind in s.kinds]


def render_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""
    parts = ["## Skills"]
    for s in skills:
        parts.append(f"### {s.name}\n{s.body}")
    return "\n\n".join(parts)


def _parse(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
        before, sep, rest = text.partition("---")
        if not sep or before.strip():  # exige '---' na primeira linha
            return None
        front, sep, body = rest.partition("---")
        if not sep:
            return None
        meta = tomllib.loads(front)
        return Skill(
            name=str(meta["name"]),
            kinds=tuple(str(k) for k in meta.get("kinds", [])),
            description=str(meta.get("description", "")),
            body=body.strip(),
            path=path,
        )
    except Exception:  # conteúdo é externo — nunca derrubar o caller
        return None
