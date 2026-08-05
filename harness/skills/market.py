"""Marketplace de skills: registry de terceiro -> `skills/pending/` -> aprovação humana.

Corpo de skill vai direto para o prompt do executor. Instalar skill de terceiro
é o mesmo risco de rodar script baixado — então o caminho tem dois degraus e
nenhum atalho:

1. `sync` traz o registry para `data/market/<name>` (clone raso, argv em LISTA,
   nunca `shell=True`) e `install` copia UMA skill para `skills/pending/`, que
   o loader não enxerga (`load_skills` varre só `skills/*.md` da raiz);
2. `approve` — ato humano, `--yes` no CLI — confere que o corpo não mudou desde
   a instalação, carimba os `kinds` e move para `skills/`.

Fail-closed em tudo que é entrada externa: url que não é `https://` não
sincroniza, arquivo acima de 64KB / não-UTF8 / sem `name` não instala, o nome
vira slug `[a-z0-9-]{1,48}` (é aí que `../../etc/passwd` morre, porque nome de
terceiro vira NOME DE ARQUIVO), o corpo passa por `trust_boundary.sanitize`
antes de tocar o disco e o resultado só é gravado se `loader._parse` aceitar —
a mesma régua de `transfer._valid_skill`, não uma segunda.

Arquivo irmão (`scripts/`, `*.py`, `*.sh`) NÃO é copiado: instalar guidance é
uma coisa, instalar executável de terceiro é outra. Eles voltam em
`ignored_files` para quem quiser buscar à mão, com os olhos abertos.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from harness import paths, trust_boundary
from harness.skills.loader import _parse, default_root

CONFIG_FILE = "skills_market.toml"
MARKET_SUBDIR = "market"
PENDING_SUBDIR = "pending"
SKILL_FILE = "SKILL.md"

# 64KB: skill é guidance, não dataset. Acima disso ou é o arquivo errado ou é
# alguém tentando encher o contexto do executor.
MAX_SKILL_BYTES = 64 * 1024
CLONE_TIMEOUT = 120
SLUG_MAX = 48
# Teto da lista de irmãos ignorados: o dir da skill pode ser um repo inteiro, e
# despejar 2000 paths no relatório não ajuda ninguém a decidir nada.
MAX_IGNORED = 50

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Registry:
    name: str
    type: str
    url: str = ""
    ref: str = "main"
    path: str = ""


# --- config ----------------------------------------------------------------


def load_registries(config_path: Path | None = None) -> list[Registry]:
    """Lê `config/skills_market.toml`. Arquivo ausente/torto => [].

    Entrada recusada é entrada DESCARTADA, não corrigida: `type="git"` com url
    fora de `https://` some da lista (fail-closed) e o `sync` correspondente
    falha com "desconhecido" em vez de baixar por um canal que qualquer um no
    caminho reescreve.
    """
    alvo = paths.config_file(CONFIG_FILE) if config_path is None else Path(config_path)
    try:
        data = tomllib.loads(alvo.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    out: list[Registry] = []
    vistos: set[str] = set()
    for raw in data.get("registry") or []:
        reg = _registry(raw)
        if reg is None or reg.name in vistos:
            continue
        vistos.add(reg.name)
        out.append(reg)
    return out


def _registry(raw: object) -> Registry | None:
    """Uma entrada `[[registry]]` validada, ou None se não passa."""
    if not isinstance(raw, dict):
        return None
    name = _slug(str(raw.get("name") or ""))
    tipo = str(raw.get("type") or "git").strip().lower()
    if not name:
        return None
    if tipo == "git":
        url = str(raw.get("url") or "").strip()
        if not url.startswith("https://"):  # fail-closed: http/ssh/file não entram
            return None
        return Registry(name=name, type="git", url=url, ref=str(raw.get("ref") or "main").strip())
    if tipo == "path":
        origem = str(raw.get("path") or "").strip()
        if not origem:
            return None
        return Registry(name=name, type="path", path=origem)
    return None


def market_dir() -> Path:
    """`data/market` — cópia local dos registries, descartável a qualquer hora."""
    return paths.data_dir() / MARKET_SUBDIR


# --- sync ------------------------------------------------------------------


def sync(name: str) -> dict:
    """Traz/atualiza um registry para `data/market/<name>`.

    Git é `subprocess.run` com argv em lista e timeout: nada de `shell=True`
    com url de config. A troca do diretório é atômica (ver `_replace_dir`) —
    clone interrompido nunca vira registry meio escrito que o `search` indexa.
    """
    reg = next((r for r in load_registries() if r.name == name), None)
    if reg is None:
        raise ValueError(f"registry '{name}' desconhecido ou recusado em {CONFIG_FILE}")
    dest = market_dir() / reg.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{reg.name}-"))
    try:
        src = tmp / "repo"
        if reg.type == "git":
            proc = subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    reg.ref,
                    "--single-branch",
                    reg.url,
                    str(src),
                ],
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT,
            )
            if proc.returncode != 0:
                erro = (proc.stderr or proc.stdout or "").strip().splitlines()
                raise RuntimeError(f"git clone falhou ({reg.url}): {erro[-1] if erro else '?'}")
        else:
            origem = Path(reg.path).expanduser()
            if not origem.is_dir():
                raise ValueError(f"registry '{reg.name}': '{origem}' não é diretório")
            shutil.copytree(origem, src)
        if not src.is_dir():
            raise RuntimeError(f"registry '{reg.name}': clone não produziu diretório")
        _replace_dir(src, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"registry": reg.name, "skills": len(_index(reg.name, dest))}


def _replace_dir(src: Path, dest: Path) -> None:
    """Troca `dest` por `src` sem janela de diretório meio escrito.

    `os.replace` não sobrescreve dir não vazio, então o antigo sai de cena por
    rename (instantâneo) e só depois é apagado: morrendo no meio, o que fica é
    o novo inteiro ou o antigo inteiro, nunca a mistura.
    """
    velho = None
    if dest.exists():
        velho = dest.parent / f".old-{dest.name}-{os.getpid()}"
        shutil.rmtree(velho, ignore_errors=True)
        os.replace(dest, velho)
    os.replace(src, dest)
    if velho is not None:
        shutil.rmtree(velho, ignore_errors=True)


# --- busca -----------------------------------------------------------------


def search(term: str = "") -> list[dict]:
    """Casa `term` em name/description das skills já sincronizadas.

    Termo vazio lista tudo. Cada achado traz o `id` (`<registry>/<slug>`) que
    `install` consome — quem procura não precisa saber onde o arquivo caiu.
    """
    alvo = (term or "").strip().lower()
    base = market_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for reg in sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for entry in _index(reg, base / reg):
            if alvo and alvo not in f"{entry['name']} {entry['description']}".lower():
                continue
            out.append(entry)
    return out


def _index(registry: str, root: Path) -> list[dict]:
    """Todo `.md` com frontmatter e `name` sob `root` (SKILL.md inclusive).

    Id duplicado (dois dirs com skills de mesmo nome) fica com o primeiro em
    ordem de path: escolha determinística vale mais que escolha esperta.
    """
    if not root.is_dir():
        return []
    out: list[dict] = []
    vistos: set[str] = set()
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if not path.is_file() or any(parte.startswith(".") for parte in rel.parts):
            continue
        texto = _read_text(path)
        if texto is None:
            continue
        meta, _ = _frontmatter(texto)
        nome = str(meta.get("name") or "").strip()
        slug = _slug(nome)
        if not nome or not slug:
            continue
        sid = f"{registry}/{slug}"
        if sid in vistos:
            continue
        vistos.add(sid)
        out.append(
            {
                "id": sid,
                "name": nome,
                "description": str(meta.get("description") or "").strip(),
                "path": str(path),
            }
        )
    return out


def _read_text(path: Path) -> str | None:
    """Conteúdo utf-8 dentro do teto, ou None. Arquivo externo nunca levanta."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_SKILL_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- instalação ------------------------------------------------------------


def install(skill_id: str, root: Path | str | None = None) -> dict:
    """Copia uma skill do market para `skills/pending/<slug>.md`.

    Pending é o ponto: o loader não lê esse dir, então instalar NÃO habilita
    nada — só põe o texto onde um humano consegue ler antes de aprovar. Toda
    recusa volta como `status="skipped"` + `reason`, nunca exceção: quem chama
    em lote precisa do motivo por item.
    """
    entry = next((e for e in search("") if e["id"] == skill_id), None)
    if entry is None:
        return _skip(skill_id, "", "id-desconhecido")
    src = Path(entry["path"])
    try:
        raw = src.read_bytes()
    except OSError:
        return _skip(skill_id, "", "ilegivel")
    # Revalidado aqui e não só no índice: entre o `search` e o `install` o
    # arquivo pode ter sido trocado (e o índice pode nem ter passado por aqui).
    if len(raw) > MAX_SKILL_BYTES:
        return _skip(skill_id, "", "acima-de-64kb")
    try:
        texto = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _skip(skill_id, "", "nao-utf8")
    meta, corpo = _frontmatter(texto)
    nome = str(meta.get("name") or "").strip()
    if not nome:
        return _skip(skill_id, "", "sem-name")
    slug = _slug(nome)
    if not slug:
        return _skip(skill_id, "", "name-invalido")
    pend = pending_dir(root)
    alvo = pend / f"{slug}.md"
    if alvo.exists():
        return _skip(skill_id, slug, "ja-em-pending")
    if (skills_dir(root) / f"{slug}.md").exists():
        return _skip(skill_id, slug, "ja-instalada")
    corpo = trust_boundary.sanitize(corpo).strip()
    conteudo = _render(
        {
            "name": nome,
            "description": str(meta.get("description") or "").strip(),
            "kinds": [],  # kind é decisão de quem aprova, não do registry
            "origin": _origin(src, entry["id"]),
            "origin_sha256": body_sha(corpo),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "approved": False,
        },
        corpo,
    )
    if not _valid_skill(conteudo):
        return _skip(skill_id, slug, "frontmatter-invalido")
    pend.mkdir(parents=True, exist_ok=True)
    alvo.write_text(conteudo, encoding="utf-8")
    return {
        "id": skill_id,
        "slug": slug,
        "status": "installed",
        "reason": "",
        "path": str(alvo),
        "sha256": body_sha(corpo),
        "ignored_files": _siblings(src),
    }


def approve(slug: str, kinds: list[str] | None = None, root: Path | str | None = None) -> dict:
    """Move `pending/<slug>.md` para `skills/<slug>.md` com os `kinds` dados.

    Recalcula o sha do corpo e compara com `origin_sha256`: o que o humano
    aprova tem que ser o que foi instalado. Divergiu (edição no pending, disco
    torto, patch de terceiro) => recusa; reaprovar de propósito é reinstalar.
    """
    slug = _slug(slug)
    src = pending_dir(root) / f"{slug}.md"
    if not slug or not src.is_file():
        return {"slug": slug, "status": "error", "reason": "nao-esta-em-pending", "path": ""}
    skill = _parse(src)
    front = _front(src)
    if skill is None or front is None:
        return {"slug": slug, "status": "error", "reason": "frontmatter-invalido", "path": ""}
    esperado = str(front.get("origin_sha256") or "")
    if not esperado or esperado != body_sha(skill.body):
        return {"slug": slug, "status": "error", "reason": "sha-divergente", "path": ""}
    dest = skills_dir(root) / f"{slug}.md"
    if dest.exists():
        return {"slug": slug, "status": "error", "reason": "ja-existe", "path": str(dest)}
    novo = dict(front)
    novo["kinds"] = [str(k) for k in (kinds or [])]
    novo["approved"] = True
    conteudo = _render(novo, skill.body)
    if not _valid_skill(conteudo):
        return {"slug": slug, "status": "error", "reason": "frontmatter-invalido", "path": ""}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(conteudo, encoding="utf-8")
    src.unlink()
    return {
        "slug": slug,
        "status": "approved",
        "reason": "",
        "path": str(dest),
        "kinds": list(novo["kinds"]),
    }


def installed(root: Path | str | None = None) -> list[dict]:
    """Pending + aprovadas que vieram do market, com origem e sha.

    Em `skills/` só entra quem tem `origin`: skill escrita aqui (ou minerada
    pelo loop) não é do market e não cabe neste inventário. Em `pending/` entra
    tudo — nada mais escreve lá.
    """
    out: list[dict] = []
    for status, base in (("pending", pending_dir(root)), ("approved", skills_dir(root))):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            front = _front(path)
            if front is None:
                continue
            origem = str(front.get("origin") or "")
            if status == "approved" and not origem:
                continue
            out.append(
                {
                    "slug": path.stem,
                    "name": str(front.get("name") or ""),
                    "status": status,
                    "kinds": [str(k) for k in front.get("kinds", [])],
                    "origin": origem,
                    "origin_sha256": str(front.get("origin_sha256") or ""),
                    "path": str(path),
                }
            )
    return out


# --- caminhos --------------------------------------------------------------


def skills_dir(root: Path | str | None = None) -> Path:
    """`skills/` do harness. `root` é a RAIZ (contém `skills/`), como no transfer.

    Sem `root`, a mesma resolução de `load_skills` — instalar em um lugar e o
    loader ler outro seria pior que não instalar.
    """
    return default_root() if root is None else Path(root) / "skills"


def pending_dir(root: Path | str | None = None) -> Path:
    """`skills/pending/` — quarentena. O loader varre `skills/*.md`, não subdir."""
    return skills_dir(root) / PENDING_SUBDIR


# --- frontmatter -----------------------------------------------------------


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Frontmatter YAML-mínimo -> (meta, corpo). Sem PyYAML, de propósito.

    Entende só o que uma skill precisa: `chave: valor`, lista inline
    (`chave: [a, b]`) e lista em bloco (`- item`). O resto é ignorado — YAML
    completo é uma linguagem, e interpretar linguagem de terceiro não é o
    negócio deste módulo.
    """
    before, sep, rest = text.partition("---")
    if not sep or before.strip():
        return {}, text
    front, sep, body = rest.partition("\n---")
    if not sep:
        return {}, text
    meta: dict[str, object] = {}
    lista: str | None = None
    for linha in front.splitlines():
        item = linha.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("- ") and lista is not None:
            meta.setdefault(lista, [])
            valores = meta[lista]
            if isinstance(valores, list):
                valores.append(_unquote(item[2:]))
            continue
        chave, sep2, valor = item.partition(":")
        lista = None
        if not sep2 or not chave.strip():
            continue
        chave, valor = chave.strip(), valor.strip()
        if not valor:
            lista, meta[chave] = chave, []
        elif valor.startswith("[") and valor.endswith("]"):
            meta[chave] = [_unquote(v) for v in valor[1:-1].split(",") if v.strip()]
        else:
            meta[chave] = _unquote(valor)
    return meta, body.lstrip("\n")


def _unquote(valor: str) -> str:
    v = valor.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
        return v[1:-1]
    return v


def _front(path: Path) -> dict | None:
    """Frontmatter TOML de uma skill NOSSA (pending/skills), ou None."""
    texto = _read_text(path)
    if texto is None:
        return None
    before, sep, rest = texto.partition("---")
    if not sep or before.strip():
        return None
    front, sep, _ = rest.partition("---")
    if not sep:
        return None
    try:
        return tomllib.loads(front)
    except tomllib.TOMLDecodeError:
        return None


def _render(front: dict, body: str) -> str:
    """Frontmatter TOML + corpo, no formato que `loader._parse` lê."""
    linhas = [f"{chave} = {_toml_value(valor)}" for chave, valor in front.items()]
    return "---\n" + "\n".join(linhas) + "\n---\n" + body.strip() + "\n"


def _toml_value(valor: object) -> str:
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, (list, tuple)):
        return "[" + ", ".join(_toml_str(str(v)) for v in valor) + "]"
    return _toml_str(str(valor))


def _toml_str(valor: str) -> str:
    """String TOML de uma linha só. Controle vira espaço, aspas/barra escapam.

    O valor vem do frontmatter de terceiro: sem isto, uma description com `"`
    (ou com `\\n`) fecha a string e o frontmatter inteiro deixa de parsear —
    ou, pior, injeta uma chave.
    """
    limpo = _CTRL_RE.sub(" ", valor).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{limpo}"'


def _valid_skill(conteudo: str) -> bool:
    """Vale o que o loader consegue parsear — mesma régua de `transfer._valid_skill`."""
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.md"
        try:
            probe.write_text(conteudo, encoding="utf-8")
        except OSError:
            return False
        return _parse(probe) is not None


# --- utilidades ------------------------------------------------------------


def _slug(nome: str) -> str:
    """`[a-z0-9-]{1,48}`, ou "" quando não sobra nada.

    É aqui que `../../etc/passwd` morre: o `name` vem do frontmatter de
    terceiro e vira NOME DE ARQUIVO. Não sanitizar path, ELIMINAR tudo que não
    é do alfabeto — lista de proibidos sempre esquece um caso.
    """
    limpo = _SLUG_RE.sub("-", (nome or "").lower()).strip("-")
    return limpo[:SLUG_MAX].strip("-")


def body_sha(body: str) -> str:
    """sha256 do CORPO como ele fica instalado (pós-`sanitize`, `strip`ado).

    Não é o sha do arquivo de origem de propósito: entre install e approve o
    frontmatter é reescrito e as tags do corpo são neutralizadas, então um sha
    do byte original seria impossível de recalcular — e um campo que ninguém
    verifica não protege nada. O que este prova é o que importa na aprovação:
    "o corpo que você está aprovando é o corpo que foi instalado".
    """
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def _origin(src: Path, fallback: str) -> str:
    """Path da skill relativo a `data/market`, para auditar de onde ela veio."""
    try:
        return str(src.relative_to(market_dir()))
    except ValueError:
        return fallback


def _siblings(src: Path) -> list[str]:
    """Arquivos que acompanham a skill e NÃO foram copiados.

    Só para `SKILL.md`, onde o diretório É o pacote da skill; para um `.md`
    solto os vizinhos são de outra pessoa e listá-los é ruído.
    """
    if src.name != SKILL_FILE:
        return []
    base = src.parent
    out = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and path != src:
            out.append(str(path.relative_to(base)))
        if len(out) >= MAX_IGNORED:
            break
    return out


def _skip(skill_id: str, slug: str, motivo: str) -> dict:
    return {
        "id": skill_id,
        "slug": slug,
        "status": "skipped",
        "reason": motivo,
        "path": "",
        "sha256": "",
        "ignored_files": [],
    }
