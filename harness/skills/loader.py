"""Skills: guidance em markdown + frontmatter TOML, injetada no system prompt.

Formato compartilhado: primeira linha '---', linhas TOML (name/kinds/description),
'---', corpo markdown. Arquivo malformado é pulado — loader nunca levanta.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_PACKAGE_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def default_root() -> Path:
    """`skills/` da raiz do harness, resolvido em CALL-TIME.

    Constante `Path("skills")` era relativa ao cwd: qualquer chamador que rode
    de fora do repo (ou com o cwd no workspace) carregava zero skill em
    silêncio. Ordem: `$HARNESS_ROOT` (mesmo mecanismo de cli/doctor, e é o que
    o teste aponta pro tmpdir) > `skills/` do cwd > `skills/` ao lado do
    pacote. O env, quando setado, MANDA — inclusive quando não tem skill
    nenhuma lá, senão isolamento de teste vazaria pro repo real.
    """
    from harness.improve import ROOT_ENV, root_dir

    if os.environ.get(ROOT_ENV):
        return root_dir() / "skills"
    cwd_root = Path("skills")
    return cwd_root if cwd_root.is_dir() else _PACKAGE_SKILLS


@dataclass(frozen=True)
class Skill:
    name: str
    kinds: tuple[str, ...]
    description: str
    body: str
    path: Path


def load_skills(root: Path | None = None) -> list[Skill]:
    """Dir ausente => []. Arquivo malformado => pulado."""
    root = default_root() if root is None else root
    if not root.is_dir():
        return []
    out: list[Skill] = []
    for path in sorted(root.glob("*.md")):
        skill = _parse(path)
        if skill is not None:
            out.append(skill)
    return out


def select_skills(kind: str | None, root: Path | None = None) -> list[Skill]:
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
