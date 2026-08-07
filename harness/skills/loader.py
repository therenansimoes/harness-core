"""Skills: guidance em markdown + frontmatter TOML, injetada no system prompt.

Formato compartilhado: primeira linha '---', linhas TOML (name/kinds/description),
'---', corpo markdown. Arquivo malformado é pulado — loader nunca levanta.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from harness import paths

_log = logging.getLogger(__name__)

# Ranking: contexto é o recurso mais caro do executor pequeno. Mandar todas as
# skills do kind (4 skills / 5.5KB num run medido, metade irrelevante pra tarefa)
# custa mais do que a guidance vale. Teto padrão de 3.
# ym0.18 redesign: SELECT_LIMIT=3 so PATH_CAP=max(1,limit//3)=1 leaves 2 free
# desc-ranked slots — stops alphabet path-flood without starving multi-file procs.
SELECT_LIMIT = 3
# ym0.20: content tasks load proc-content-cta-skeleton (path-triggered, slot 1)
# + up to 2 methodology bodies at SELECT_LIMIT=3, inflating wall ~73s.
# Reducing content limit to 2 keeps proc + 1 best-ranked methodology instead of 2.
CONTENT_SELECT_LIMIT = 2

_TOKEN = re.compile(r"\w{3,}", re.UNICODE)

# Palavras que aparecem em quase toda skill/prompt: se contassem, o score viraria
# ruído e o ranking, ordem alfabética.
_STOPWORDS = frozenset(
    [
        "para",
        "com",
        "sem",
        "por",
        "que",
        "uma",
        "que",
        "dos",
        "das",
        "nos",
        "nas",
        "ele",
        "ela",
        "isso",
        "este",
        "esta",
        "esse",
        "essa",
        "aqui",
        "não",
        "nao",
        "sim",
        "mais",
        "menos",
        "como",
        "quando",
        "onde",
        "qual",
        "todo",
        "toda",
        "todos",
        "todas",
        "ser",
        "seu",
        "sua",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "you",
        "your",
        "are",
        "was",
        "not",
        "but",
        "any",
        "all",
        "use",
        "using",
        "when",
        "where",
        "what",
        "which",
    ]
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


def default_root() -> Path:
    """`skills/` da raiz do harness, resolvido em CALL-TIME.

    Constante `Path("skills")` era relativa ao cwd: qualquer chamador que rode
    de fora do repo (ou com o cwd no workspace) carregava zero skill em
    silêncio. Ordem: `$HARNESS_ROOT` (mesmo mecanismo de cli/doctor, e é o que
    o teste aponta pro tmpdir) > o que `paths.skills_dir()` resolver (`skills/`
    irmão do config em uso > `skills/` empacotada). O env, quando setado,
    MANDA — inclusive quando não tem skill nenhuma lá, senão isolamento de
    teste vazaria pro repo real.
    """
    from harness.improve import ROOT_ENV, root_dir

    if os.environ.get(ROOT_ENV):
        return root_dir() / "skills"
    return paths.skills_dir()


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
    2. Ranking por relevância à unidade: tokens em comum entre `query` e a
       `description` (primeiro), depois o corpo só como desempate. Barato e
       determinístico — sem embedding. Empate final mantém ordem de
       `load_skills` (path / alfabética).

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

    def _desc_score(s: Skill) -> tuple[int, int]:
        d = len(wanted & _tokens(s.description)) if wanted else 0
        b = len(wanted & _tokens(s.body)) if wanted else 0
        return (d, b)

    # ym0.18 redesign: rank triggered by description score THEN cap at PATH_CAP.
    # PATH_CAP = max(1, limit // 3) — at SELECT_LIMIT=3 this is 1, leaving 2 free
    # desc-ranked slots so multi-file procs/methodology are never starved.
    # Skills that were path-triggered but exceed PATH_CAP fall back into resto
    # for desc-ranking (they compete on merit, not alphabetic arrival order).
    if triggered:
        triggered = sorted(triggered, key=lambda s: (-_desc_score(s)[0], -_desc_score(s)[1]))
        path_cap = max(1, limit // 3)
        overflow = triggered[path_cap:]
        triggered = triggered[:path_cap]
        resto = overflow + resto

    if wanted:
        # Description first (dense frontmatter), body as tie-break only.
        # Scoring description+body equally let long skills (ledger, langgraph)
        # crowd out short methodology ones on shared stopwords.
        scored = [(_desc_score(s), s) for s in resto]
        if any(sc[0] or sc[1] for sc, _ in scored):
            # If anyone hit on description, drop body-only noise (long skills).
            if any(sc[0] for sc, _ in scored):
                scored = [(sc, s) for sc, s in scored if sc[0]]
            else:
                scored = [(sc, s) for sc, s in scored if sc[0] or sc[1]]
            resto = [s for _, s in sorted(scored, key=lambda pair: (-pair[0][0], -pair[0][1]))]
    result = (triggered + resto)[: max(limit, 0)]
    # Fail-open: log selection for observability (ym0.16 audit). Never raises.
    try:
        _log.debug(
            "select_skills kind=%r limit=%d triggered=%d total_matched=%d selected=[%s]",
            kind,
            limit,
            len(triggered),
            len(matched),
            ", ".join(
                f"{s.name}(path)" if s in triggered else s.name for s in result
            ),
        )
        if len(triggered) >= limit and limit > 0:
            crowded = [s.name for s in (triggered + resto)[limit:] if s not in triggered]
            if crowded:
                _log.debug(
                    "select_skills PATH-FLOOD: %d triggers (after PATH_CAP) fill all %d slots; "
                    "crowded_out=%s",
                    len(triggered),
                    limit,
                    crowded,
                )
    except Exception:
        pass
    return result


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


# ym0.21 E3 DISCARD: 150w cap for Qwopus 4B rejected — 9/12 accept, 3 verify failures.
# Revert to 500w default: render-time only — skill .md files unchanged.
# POST_E3: control = POST_LIMIT3 + ym0.20 CONTENT_SELECT_LIMIT=2 + body cap 500
_BODY_WORD_LIMIT = 500


def _truncate_body(body: str) -> str:
    """Truncate body to _BODY_WORD_LIMIT words at the last ## section boundary.

    If no ## boundary exists before the limit, hard-truncates at the word limit.
    Preserves the last ## Done when section when it falls near the boundary.
    """
    words = body.split()
    if len(words) <= _BODY_WORD_LIMIT:
        return body
    # Find the last '## ' heading that starts before the word-limit boundary.
    # Reconstruct char position of the limit word.
    limit_pos = len(" ".join(words[:_BODY_WORD_LIMIT]))
    boundary = body.rfind("\n##", 0, limit_pos)
    if boundary > 0:
        return body[:boundary].rstrip() + "\n\n<!-- skill truncated -->"
    return " ".join(words[:_BODY_WORD_LIMIT]) + " <!-- skill truncated -->"


def render_prompt(skills: list[Skill]) -> str:
    if not skills:
        return ""
    parts = ["## Skills"]
    for s in skills:
        parts.append(f"### {s.name}\n{_truncate_body(s.body)}")
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
