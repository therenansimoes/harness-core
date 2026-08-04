"""Skills: guidance em markdown + frontmatter TOML, injetada no system prompt.

Formato compartilhado: primeira linha '---', linhas TOML (name/kinds/description),
'---', corpo markdown. Arquivo malformado é pulado — loader nunca levanta.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

_PACKAGE_SKILLS = Path(__file__).resolve().parents[2] / "skills"

# Ranking: contexto é o recurso mais caro do executor pequeno. Mandar todas as
# skills do kind (4 skills / 5.5KB num run medido, metade irrelevante pra tarefa)
# custa mais do que a guidance vale. Teto padrão de 2.
SELECT_LIMIT = 2

_TOKEN = re.compile(r"\w{3,}", re.UNICODE)

# Palavras que aparecem em quase toda skill/prompt: se contassem, o score viraria
# ruído e o ranking, ordem alfabética.
_STOPWORDS = frozenset(
    """
    para com sem por que uma que dos das nos nas ele ela isso este esta esse essa
    aqui não nao sim mais menos como quando onde qual todo toda todos todas ser
    seu sua the and for with that this from into you your are was not but any all
    use using when where what which
    """.split()
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


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
    # Globs de arquivo que disparam a skill sem passar pelo ranking fuzzy.
    paths: tuple[str, ...] = ()


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


def select_skills(
    kind: str | None,
    root: Path | None = None,
    *,
    query: str | None = None,
    files: list[str] | None = None,
    limit: int = SELECT_LIMIT,
) -> list[Skill]:
    """`kinds` vazio = vale para todo kind; kind None só casa com esses.

    Depois do filtro por kind vêm dois eixos, nesta ordem:

    1. Path-trigger determinístico: skill com `paths` (globs no frontmatter) cujo
       glob casa algum arquivo de `files` — os alvos conhecidos da unidade
       (`files_changed`, path do pedido) — entra ANTES do ranking. Sem `files` o
       eixo nem roda, então o comportamento de quem não passa nada é o de antes.
    2. Ranking por relevância à unidade: score = tokens em comum entre `query` (o
       prompt da unidade) e `description + corpo` da skill. Barato e
       determinístico — sem embedding, sem chamada de modelo. Empate mantém a
       ordem de `load_skills` (path, i.e. alfabética).

    Skill sem nenhum token em comum sai fora QUANDO alguma outra pontuou; se
    ninguém pontuou (ou não veio query utilizável) cai no comportamento por kind
    puro, cortado no mesmo teto. Skill global (`kinds = []`) compete no mesmo
    ranking: não passa de graça só por não ter restrição de kind.

    O teto `limit` continua valendo para o total: path-trigger fura a FILA, não
    o orçamento de contexto.
    """
    matched = [s for s in load_skills(root) if not s.kinds or kind in s.kinds]
    triggered = [s for s in matched if _path_hit(s, files)]
    resto = [s for s in matched if s not in triggered]
    wanted = _tokens(query) if query else set()
    if wanted:
        scored = [(len(wanted & _tokens(f"{s.description}\n{s.body}")), s) for s in resto]
        if any(score for score, _ in scored):
            scored = [(score, s) for score, s in scored if score]
            resto = [s for _, s in sorted(scored, key=lambda pair: -pair[0])]
    return (triggered + resto)[: max(limit, 0)]


def _path_hit(skill: Skill, files: list[str] | None) -> bool:
    """True se algum glob da skill casa algum arquivo da unidade.

    Casa contra o path como veio E contra o basename: `paths = ["*.toml"]` tem
    que pegar `config/agents.toml` e `agents.toml` igual, senão o gatilho
    depende de o chamador ter normalizado o path."""
    if not skill.paths or not files:
        return False
    for arquivo in files:
        alvo = str(arquivo)
        nome = PurePosixPath(alvo).name
        if any(fnmatch(alvo, glob) or fnmatch(nome, glob) for glob in skill.paths):
            return True
    return False


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
            paths=tuple(str(p) for p in meta.get("paths", [])),
        )
    except Exception:  # conteúdo é externo — nunca derrubar o caller
        return None
