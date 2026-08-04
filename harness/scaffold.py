"""Scaffold de projeto e geração de asset SVG para o executor.

Duas tools que substituem a mesma dor por caminhos diferentes:

- `scaffold` copia um template CURADO do repo (`templates/`, catálogo em
  `templates/catalog.toml`). Motivo de existir: o modelo pequeno sabe escrever
  um `index.html`, mas o que ele escreve é sempre o mesmo esqueleto de tutorial
  — `<div>` no lugar de landmark, cor chumbada, sem skip-link, sem viewport. O
  template resolve isso de uma vez e gasta ZERO token de geração.
- `asset_gen` desenha SVG por f-string. Motivo: pedir ícone pro LLM produz path
  inventado que não fecha, e "arruma o path" queima turno atrás de turno. Aqui a
  geometria é código: sempre parseia, sempre fecha, e a paleta sai do
  `tokens.css` do workspace quando existe (asset combinando com o tema, não
  cinza aleatório).

Contrato dos dois (o mesmo do resto das tools):
- jail: destino resolvido SOB o workspace; `..` ou path absoluto vira erro;
- `scaffold` recusa destino que já tem conteúdo e NÃO escreve nada nesse caso —
  meio-scaffold sobre projeto existente é o pior estado possível;
- SVG é validado com `xml.etree` ANTES de gravar: arquivo inválido nunca chega
  ao disco;
- erro é STRING de retorno, nunca exceção: exceção em tool node derruba o run.
"""

from __future__ import annotations

import re
import shutil
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# templates/ é irmão de harness/ no repo (dado versionado, não código).
TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"
CATALOG_FILE = "catalog.toml"

# Nome de projeto: sem separador de path, sem `..`, sem ponto inicial. É a
# defesa contra traversal ANTES de qualquer resolve — recusar cedo dá mensagem
# melhor que "path fora do workspace".
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Lixo que nunca deve viajar do template para o workspace.
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "node_modules", ".git")

# Paleta de fallback do `asset_gen`, em hex de propósito: o `tokens.css` do
# template usa `oklch()`, que browser moderno renderiza mas rasterizador antigo
# não. Sem tokens no workspace, hex é a aposta que sempre abre.
DEFAULT_PALETTE = {
    "accent": "#6d4aff",
    "surface": "#ffffff",
    "text": "#2b2440",
    "muted": "#6f6a85",
    "border": "#ddd8ea",
}

# Onde procurar tokens.css no workspace, em ordem de preferência.
_TOKENS_CANDIDATES = ("tokens.css", "src/tokens.css", "css/tokens.css", "styles/tokens.css")

ASSETS_DIR = "assets"

ICON_SIZE = 24  # viewBox 24: grade de ícone padrão, stroke de 2 fica nítido
ICON_STROKE = 2.0
DEFAULT_PLACEHOLDER = (800, 450)  # 16:9
LOGO_SIZE = 96


class _Refused(Exception):
    """Pedido recusado (kind inválido, destino ocupado, traversal). Virou string
    na borda da tool."""


# --------------------------------------------------------------------------- #
# catálogo
# --------------------------------------------------------------------------- #


def load_catalog(root: Path | None = None) -> dict:
    """Catálogo de templates indexado por `kind`. `{}` se o arquivo não existe."""
    base = Path(root) if root else TEMPLATES_ROOT
    path = base / CATALOG_FILE
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def catalog_manual(root: Path | None = None) -> str:
    """Uma linha por kind, para o prompt e para a mensagem de erro."""
    cat = load_catalog(root)
    if not cat:
        return "(nenhum template disponível)"
    return "\n".join(
        f"- {kind}: {entry.get('summary', '')} {entry.get('use', '')}".rstrip()
        for kind, entry in sorted(cat.items())
    )


# --------------------------------------------------------------------------- #
# scaffold
# --------------------------------------------------------------------------- #


def _dest(ws: Path, name: str) -> Path:
    """Destino validado sob o workspace. Levanta `_Refused` em traversal."""
    if not name or not _NAME_OK.match(name):
        raise _Refused(
            f"name inválido: {name!r}. Use só letras, números, ponto, hífen e underscore "
            "(sem `/`, sem `..`, sem path absoluto) — o scaffold cria UMA pasta no "
            "workspace, não em qualquer lugar do disco."
        )
    root = ws.resolve()
    dest = (root / name).resolve()
    # Cinto e suspensório: o regex já barra separador, mas o jail é o que vale.
    if dest != root and root not in dest.parents:
        raise _Refused(f"destino fora do workspace: {dest}")
    return dest


def _ocupado(dest: Path) -> bool:
    if not dest.exists():
        return False
    if dest.is_file():
        return True
    return any(dest.iterdir())


def scaffold(kind: str, name: str, ws: str | Path) -> str:
    """Copia o template `kind` para `<ws>/<name>` e lista o que criou.

    Recusa (sem escrever nada) kind fora do catálogo, name com traversal e
    destino que já tem conteúdo.
    """
    ws_path = Path(ws)
    cat = load_catalog()
    entry = cat.get(kind)
    if entry is None:
        disponiveis = ", ".join(sorted(cat)) or "(catálogo vazio)"
        raise _Refused(f"kind desconhecido: {kind!r}. Disponíveis: {disponiveis}.")

    src = TEMPLATES_ROOT / str(entry.get("dir") or kind)
    if not src.is_dir():
        raise _Refused(f"template {kind!r} está no catálogo mas a pasta {src} não existe.")

    dest = _dest(ws_path, name)
    if _ocupado(dest):
        raise _Refused(
            f"destino já tem conteúdo: {name}/ — nada foi escrito. Escolha outro nome ou "
            "edite o que já está lá (scaffold por cima de projeto existente mistura dois "
            "esqueletos e ninguém desfaz isso depois)."
        )

    shutil.copytree(src, dest, ignore=_IGNORE, dirs_exist_ok=True)
    criados = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    lista = "\n".join(f"  {name}/{f}" for f in criados)
    proximos = entry.get("next") or []
    passos = "".join(f"\n  $ {c}" for c in proximos)
    return f"scaffold ok: {kind} → {name}/ ({len(criados)} arquivos)\n{lista}" + (
        f"\nPróximo passo (rode na pasta {name}/):{passos}" if passos else ""
    )


# --------------------------------------------------------------------------- #
# paleta
# --------------------------------------------------------------------------- #

_VAR_RE = {
    key: re.compile(rf"--{key}\s*:\s*([^;]+);", re.IGNORECASE)
    for key in ("accent", "surface", "text", "border")
}
_MUTED_RE = re.compile(r"--text-muted\s*:\s*([^;]+);", re.IGNORECASE)
_LIGHT_DARK_RE = re.compile(r"light-dark\(\s*([^,]+?)\s*,", re.IGNORECASE | re.DOTALL)


def _unwrap(valor: str) -> str:
    """Valor de token pronto para atributo SVG.

    `light-dark(a, b)` só resolve em documento com `color-scheme`; SVG solto não
    tem isso, então fica o lado CLARO — asset em página escura continua legível
    porque o desenho tem borda própria, o contrário (escuro em claro) não.
    """
    valor = " ".join(valor.split())
    m = _LIGHT_DARK_RE.search(valor)
    if m:
        valor = m.group(1).strip()
    return valor


def _resolve_hue(valor: str, tokens_src: str) -> str:
    """Substitui `var(--hue)` pelo número literal — SVG não herda a variável."""
    if "var(--hue" not in valor:
        return valor
    hue = re.search(r"--hue\s*:\s*([\d.]+)\s*;", tokens_src)
    if not hue:
        return ""
    return re.sub(r"var\(\s*--hue[^)]*\)", hue.group(1), valor)


def palette(ws: str | Path) -> dict[str, str]:
    """Paleta do workspace, lida do `tokens.css` se houver; default se não."""
    out = dict(DEFAULT_PALETTE)
    base = Path(ws)
    for rel in _TOKENS_CANDIDATES:
        path = base / rel
        if not path.is_file():
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key, rx in _VAR_RE.items():
            m = rx.search(src)
            if not m:
                continue
            valor = _resolve_hue(_unwrap(m.group(1)), src)
            # `"` no valor viraria atributo quebrado; token estranho = ignora.
            if valor and '"' not in valor and "var(" not in valor:
                out[key] = valor
        m = _MUTED_RE.search(src)
        if m:
            valor = _resolve_hue(_unwrap(m.group(1)), src)
            if valor and '"' not in valor and "var(" not in valor:
                out["muted"] = valor
        break
    return out


# --------------------------------------------------------------------------- #
# asset_gen
# --------------------------------------------------------------------------- #


# Ícones: geometria em coordenadas da grade 24, traço aberto (nada de fill), o
# que faz o mesmo desenho funcionar em 16px e em 64px.
def _icon_body(shape: str) -> str:
    if shape == "seta":
        return '  <path d="M4 12 H19" />\n  <path d="M13 6 L19 12 L13 18" />\n'
    if shape == "check":
        return '  <path d="M4.5 13 L9.5 18 L19.5 6.5" />\n'
    if shape == "x":
        return '  <path d="M6 6 L18 18" />\n  <path d="M18 6 L6 18" />\n'
    if shape == "engrenagem":
        # Dentes por rotação: 8 retângulos iguais girados em torno do centro.
        # Escrever isso à mão é onde o desenho manual sempre sai torto.
        dentes = "".join(
            f'  <rect x="11" y="1.6" width="2" height="3.6" rx="0.6" '
            f'transform="rotate({ang} 12 12)" />\n'
            for ang in range(0, 360, 45)
        )
        return (
            f'{dentes}  <circle cx="12" cy="12" r="6.6" />\n  <circle cx="12" cy="12" r="2.6" />\n'
        )
    if shape == "casa":
        return (
            '  <path d="M3.5 10.5 L12 4 L20.5 10.5 V20 H3.5 Z" />\n'
            '  <path d="M9.5 20 V13.5 H14.5 V20" />\n'
        )
    if shape == "lupa":
        return '  <circle cx="10.5" cy="10.5" r="6" />\n  <path d="M15 15 L20.5 20.5" />\n'
    raise _Refused(f"forma desconhecida: {shape!r}. Disponíveis: {', '.join(ICON_SHAPES)}.")


ICON_SHAPES = ("seta", "check", "x", "engrenagem", "casa", "lupa")
LOGO_SHAPES = ("circulo", "hex")

_DIM_RE = re.compile(r"\b(\d{2,5})\s*[x×]\s*(\d{2,5})\b")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(texto: str, fallback: str) -> str:
    s = _SLUG_RE.sub("-", texto.strip().lower()).strip("-")
    return s[:48] or fallback


def _icon_svg(spec: str, pal: dict) -> tuple[str, str, str]:
    partes = spec.split()
    shape = (partes[0] if partes else "").lower()
    body = _icon_body(shape)  # levanta antes de qualquer escrita
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ICON_SIZE} {ICON_SIZE}" '
        f'width="{ICON_SIZE}" height="{ICON_SIZE}" fill="none" '
        f'stroke="{xml_escape(pal["accent"], {chr(34): "&quot;"})}" '
        f'stroke-width="{ICON_STROKE}" stroke-linecap="round" stroke-linejoin="round" '
        f'role="img" aria-label="{xml_escape(shape)}">\n'
        f"  <title>{xml_escape(shape)}</title>\n"
        f"{body}</svg>\n"
    )
    return svg, f"icon-{shape}", f"{ICON_SIZE}x{ICON_SIZE}"


def _placeholder_svg(spec: str, pal: dict) -> tuple[str, str, str]:
    m = _DIM_RE.search(spec)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        label = (spec[: m.start()] + spec[m.end() :]).strip()
    else:
        w, h = DEFAULT_PLACEHOLDER
        label = spec.strip()
    w, h = max(16, min(w, 8000)), max(16, min(h, 8000))
    label = label or "placeholder"
    # Tipo relativo ao menor lado: o mesmo SVG serve de banner e de avatar sem
    # o texto virar uma faixa gigante.
    fs = max(11, round(min(w, h) * 0.09))
    q = {chr(34): "&quot;"}
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
        f'height="{h}" role="img" aria-label="{xml_escape(label, q)} {w}x{h}">\n'
        f"  <title>{xml_escape(label)} — {w}×{h}</title>\n"
        f'  <rect width="{w}" height="{h}" fill="{xml_escape(pal["surface"], q)}" />\n'
        f'  <rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" fill="none" '
        f'stroke="{xml_escape(pal["border"], q)}" stroke-width="1" />\n'
        # Diagonais em tom fraco: é o que faz o olho ler "aqui falta imagem".
        f'  <path d="M0 0 L{w} {h} M{w} 0 L0 {h}" stroke="{xml_escape(pal["border"], q)}" '
        f'stroke-width="1" opacity="0.55" />\n'
        f'  <text x="{w / 2:.1f}" y="{h / 2:.1f}" text-anchor="middle" '
        f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="{fs}" '
        f'fill="{xml_escape(pal["text"], q)}">{xml_escape(label)}</text>\n'
        f'  <text x="{w / 2:.1f}" y="{h / 2 + fs * 1.35:.1f}" text-anchor="middle" '
        f'font-family="ui-monospace, monospace" font-size="{max(9, round(fs * 0.7))}" '
        f'fill="{xml_escape(pal["muted"], q)}">{w}×{h}</text>\n'
        f"</svg>\n"
    )
    return svg, f"placeholder-{_slug(label, 'box')}-{w}x{h}", f"{w}x{h}"


def _logo_svg(spec: str, pal: dict) -> tuple[str, str, str]:
    partes = spec.split()
    forma = "circulo"
    letras = []
    for p in partes:
        if p.lower() in LOGO_SHAPES:
            forma = p.lower()
        else:
            letras.append(p)
    # Monograma = iniciais das palavras, no máximo 2 (3 já não lê em 24px).
    mono = "".join(w[0] for w in letras if w)[:2].upper() or "A"
    s = LOGO_SIZE
    c = s / 2
    r = s * 0.46
    q = {chr(34): "&quot;"}
    if forma == "hex":
        # Hexágono de topo plano, gerado por trigonometria em vez de path
        # decorado à mão: seis vértices exatos, sem vértice "quase" no lugar.
        from math import cos, pi, sin

        pts = " ".join(
            f"{c + r * cos(pi / 180 * (60 * i)):.2f},{c + r * sin(pi / 180 * (60 * i)):.2f}"
            for i in range(6)
        )
        fundo = f'  <polygon points="{pts}" fill="{xml_escape(pal["accent"], q)}" />\n'
    else:
        fundo = (
            f'  <circle cx="{c}" cy="{c}" r="{r:.2f}" fill="{xml_escape(pal["accent"], q)}" />\n'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" width="{s}" '
        f'height="{s}" role="img" aria-label="{xml_escape(mono, q)}">\n'
        f"  <title>{xml_escape(mono)}</title>\n"
        f"{fundo}"
        # `dominant-baseline=central` + `text-anchor=middle` é o único jeito de
        # centrar texto em SVG sem medir a fonte.
        f'  <text x="{c}" y="{c}" text-anchor="middle" dominant-baseline="central" '
        f'font-family="ui-sans-serif, system-ui, sans-serif" font-weight="700" '
        f'font-size="{s * 0.42:.0f}" letter-spacing="{-s * 0.01:.2f}" '
        f'fill="{xml_escape(pal["surface"], q)}">{xml_escape(mono)}</text>\n'
        f"</svg>\n"
    )
    return svg, f"logo-{_slug(mono, 'mark')}-{forma}", f"{s}x{s}"


ASSET_KINDS = {"icon": _icon_svg, "placeholder": _placeholder_svg, "logo-mark": _logo_svg}


def asset_gen(kind: str, spec: str, ws: str | Path) -> str:
    """Gera um SVG em `<ws>/assets/` e devolve o path relativo.

    `kind`: `icon` (spec = forma: seta/check/x/engrenagem/casa/lupa),
    `placeholder` (spec = "LARGURAxALTURA rótulo") ou `logo-mark`
    (spec = "Nome [circulo|hex]").
    """
    fn = ASSET_KINDS.get(kind)
    if fn is None:
        raise _Refused(
            f"kind de asset desconhecido: {kind!r}. Disponíveis: {', '.join(sorted(ASSET_KINDS))}."
        )
    pal = palette(ws)
    svg, nome, dims = fn(str(spec or ""), pal)

    # Validação ANTES do disco: se a f-string produziu XML torto (paleta com
    # caractere estranho, rótulo hostil), o arquivo não nasce.
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        raise _Refused(f"SVG gerado ficou inválido ({exc}) — nada foi escrito.") from None

    out_dir = Path(ws) / ASSETS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{nome}.svg"
    # Não sobrescreve: asset citado num HTML que já existe continua valendo.
    n = 2
    while path.exists():
        path = out_dir / f"{nome}-{n}.svg"
        n += 1
    path.write_text(svg, encoding="utf-8")
    return (
        f"asset_gen ok: {ASSETS_DIR}/{path.name} ({kind}, {dims}, "
        f'cor {pal["accent"]}). Use com <img src="{ASSETS_DIR}/{path.name}" alt="...">.'
    )


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #


def make_scaffold_tools(ws: str | Path) -> list:
    """Tools LangChain deste módulo com o workspace fixado.

    Erro é string de retorno, nunca exceção: exceção em tool node derruba o run.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def scaffold_project(kind: str, name: str) -> str:
        """Cria um projeto novo a partir de um template curado do harness."""
        try:
            return scaffold(kind, name, base)
        except _Refused as exc:
            return f"scaffold recusado: {exc}"
        except Exception as exc:
            return f"scaffold falhou: {type(exc).__name__}: {exc}"

    def generate_asset(kind: str, spec: str) -> str:
        """Gera um SVG (ícone, placeholder ou logo) em assets/."""
        try:
            return asset_gen(kind, spec, base)
        except _Refused as exc:
            return f"asset_gen recusado: {exc}"
        except Exception as exc:
            return f"asset_gen falhou: {type(exc).__name__}: {exc}"

    kinds = ", ".join(sorted(load_catalog())) or "(catálogo vazio)"
    return [
        StructuredTool.from_function(
            func=scaffold_project,
            name="scaffold",
            description=(
                f"Cria um projeto a partir de template curado. kind: {kinds}. "
                "name é UMA pasta nova no workspace (sem `/`, sem `..`). Recusa destino que "
                "já tem conteúdo, sem escrever nada. A saída lista os arquivos criados e os "
                "comandos do próximo passo. Use ANTES de escrever HTML/config na mão: os "
                "templates já vêm com landmarks, viewport, tokens de tema e teste que passa."
            ),
        ),
        StructuredTool.from_function(
            func=generate_asset,
            name="asset_gen",
            description=(
                "Gera um SVG válido em assets/, por geometria (nada de path inventado). "
                "kind='icon' com spec da forma (seta, check, x, engrenagem, casa, lupa); "
                "kind='placeholder' com spec '1200x630 Hero' (retângulo com rótulo e "
                "dimensões); kind='logo-mark' com spec 'Nome Projeto hex' (monograma em "
                "círculo ou hexágono). A cor sai do tokens.css do workspace quando existe."
            ),
        ),
    ]


__all__ = [
    "ASSET_KINDS",
    "ICON_SHAPES",
    "TEMPLATES_ROOT",
    "asset_gen",
    "catalog_manual",
    "load_catalog",
    "make_scaffold_tools",
    "palette",
    "scaffold",
]
