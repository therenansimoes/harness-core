"""`view_render`: a tool que deixa o modelo OLHAR a tela que ele acabou de escrever.

O modelo já tinha `local_probe` para provar que a rota responde 200 e `read_file`
para reler o HTML que ele mesmo escreveu. Nenhuma das duas mostra a TELA — e o
buraco medido é esse: página com stylesheet morto responde 200 e o HTML está
lindo no `read_file`.

Aqui o caminho é o de sempre do harness: screenshot com o Chrome que já está na
máquina, e um VLM local (opcional) julgando o PNG. As cercas são as que já
existem, não novas: porta só se estiver registrada em `procs.json` desta run
(mesma cerca do `local_probe`) e `dist_path` servido em loopback com porta
efêmera (mesmo `serve()` do `ui-verify`).

Economia deliberada: PNG abaixo de 20kb é tela vazia (calibrado no ui-verify) e
sai daqui SEM gastar um token do modelo de visão. O primeiro sinal útil já está
no tamanho do arquivo.
"""

from __future__ import annotations

import re
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path

from harness.backends.procs import LOOPBACK, _as_int, _vivo, local_probe, read_procs

HARNESS_SUBDIR = ".harness"
SHOTS_SUBDIR = "shots"
MIN_SHOT_KB = 20.0
VAZIA = "tela provavelmente vazia"

_SLUG = re.compile(r"[^a-z0-9]+")


def render(
    ws: str | Path,
    port: int | None = None,
    dist_path: str | None = None,
) -> tuple[Path | None, float, str | None]:
    """(shot, kb, erro). Tira o screenshot; NÃO julga nada.

    Separado de `view_render` porque o CLI (`harness vision-judge`) precisa do
    mesmo PNG com a mesma cerca — duas implementações da cerca seriam duas
    cercas diferentes dentro de um mês.
    """
    from harness import uiverify

    base = Path(ws)
    if (port is None) == (dist_path is None):
        return None, 0.0, "view_render exige exatamente um de port= ou dist_path="

    if port is not None:
        erro = _cerca_porta(base, _as_int(port))
        if erro:
            return None, 0.0, erro
        shot = _shot_path(base, f"port-{_as_int(port)}")
        kb, falhas = uiverify._check_shot(f"http://{LOOPBACK}:{_as_int(port)}/", shot, MIN_SHOT_KB)
        return (shot if shot.is_file() else None), kb, (falhas[0] if falhas else None)

    root = (base / dist_path) if not Path(dist_path).is_absolute() else Path(dist_path)
    if not root.is_dir():
        return None, 0.0, f"view_render recusado: {dist_path} não é um diretório"
    shot = _shot_path(base, root.name or "dist")
    with uiverify.serve(root) as url:
        kb, falhas = uiverify._check_shot(url, shot, MIN_SHOT_KB)
    return (shot if shot.is_file() else None), kb, (falhas[0] if falhas else None)


def _cerca_porta(ws: Path, port: int) -> str | None:
    """A cerca do `local_probe`, palavra por palavra: só servidor DESTA run."""
    entry = next((e for e in read_procs(ws) if _as_int(e.get("port")) == port), None)
    if entry is None:
        return (
            f"view_render recusado: porta {port} não está registrada neste workspace. "
            "Só servidores subidos por start_server nesta run podem ser olhados "
            "(use start_server e a porta que ele devolveu)."
        )
    if not _vivo(_as_int(entry.get("pid"))):
        return f"view_render recusado: o processo da porta {port} não está mais vivo (id={entry.get('id')})"
    return None


def _shot_path(ws: Path, slug: str) -> Path:
    """`<ws>/.harness/shots/<slug>-<ts>.png` — um arquivo por olhada.

    Sem sobrescrever: a sequência de PNGs de um run É o histórico visual, e é o
    que um humano abre depois para ver onde a tela quebrou.
    """
    limpo = _SLUG.sub("-", slug.lower()).strip("-") or "view"
    out = ws / HARNESS_SUBDIR / SHOTS_SUBDIR / f"{limpo}-{int(time.time() * 1000)}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def make_view_tools(ws: str | Path) -> list:
    """Tools LangChain deste módulo com o workspace fixado.

    Erro é string de retorno, nunca exceção — igual ao resto dos backends.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def view_render(
        port: int | None = None,
        dist_path: str | None = None,
        question: str | None = None,
    ) -> str:
        """Tira um screenshot da página e devolve o que se vê nela."""
        try:
            shot, kb, erro = render(base, port=port, dist_path=dist_path)
        except Exception as exc:  # tool node não pode receber exceção
            return f"view_render falhou: {type(exc).__name__}: {exc}"
        if erro:
            # Tela vazia já é a resposta: não gasta o modelo de visão.
            return f"view_render: {erro}"
        assert shot is not None
        return f"view_render: {_juizo(shot, kb, question)}"

    return [
        StructuredTool.from_function(
            func=view_render,
            name="view_render",
            description=(
                "Tira um screenshot da página e descreve o que aparece NA TELA. Use "
                "depois de mexer em HTML/CSS: 200 no local_probe não prova que a "
                "página pintou (stylesheet morto responde 200 e a tela chega crua). "
                "Passe port=<porta do start_server> para um servidor desta run, ou "
                "dist_path=<dir buildado> para servir um diretório estático. "
                "question é opcional e foca o olhar (ex.: 'o menu está alinhado?')."
            ),
        ),
    ]


def _juizo(shot: Path, kb: float, question: str | None) -> str:
    from harness import vision

    veredito = vision.judge_image(shot, question=question)
    if veredito["unavailable"]:
        # Sem modelo de visão o que sobra é o fato: pintou algo. É pouco, mas é
        # verdade, e mentir para o modelo custa um turno de conserto errado.
        return (
            f"{shot.name} {kb:.1f}kb — a página renderizou (PNG acima de "
            f"{MIN_SHOT_KB:.0f}kb). {veredito['unavailable']}: sem descrição do conteúdo."
        )
    bullets = "".join(f"\n- {b}" for b in veredito["bullets"])
    return (
        f"{shot.name} {kb:.1f}kb nota={veredito['nota']:.1f} "
        f"ok={'sim' if veredito['ok'] else 'não'}{bullets}"
    )


# --------------------------------------------------------------------------- #
# inspect_dom / a11y_audit
#
# O screenshot mostra a tela e não deixa apontar o elemento; o `read_file` mostra
# o arquivo que o modelo escreveu e não mostra o que o navegador MONTOU (template
# renderizado, script que injeta nó, tag que o parser fechou sozinho). O buraco
# entre os dois é o DOM, e é o que estas duas tools abrem.
#
# O que NÃO tem aqui, de propósito: geometria e computed style de verdade. Os
# dois exigem CDP (falar protocolo com o Chrome numa websocket), e prometer
# `bbox` calculado a partir do HTML seria número inventado — o modelo gastaria um
# turno consertando um alinhamento com base em pixel que ninguém mediu. `--dump-dom`
# é uma chamada só, sem sessão, e entrega exatamente o que sabe entregar.
# --------------------------------------------------------------------------- #

DOM_TIMEOUT_S = 20
MAX_TEXT = 300
MAX_A11Y_FILES = 20
NAO_SUPORTADO = "seletor não suportado, use forma simples"
NAO_AVALIAVEL = "não avaliável"
BBOX = "bbox: indisponível (requer CDP)"
WCAG_AA = 4.5

# Só o que dá para resolver sem layout engine: valor literal escrito no `style=`
# ou numa regra literal do `<style>`. `background-color` é lido junto porque
# contraste precisa dos dois lados, mas não entra no computed do inspect_dom —
# o pedido lá é display/color/font-size.
CSS_PROPS = ("display", "color", "font-size")
CSS_FUNDO = "background-color"

VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)
# input escondido ou botão não precisa de label: o rótulo dele é o próprio value.
TIPOS_SEM_LABEL = frozenset({"hidden", "submit", "button", "reset", "image"})
ROTULO_ATTRS = ("aria-label", "aria-labelledby", "title")

_CORES = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "lime": (0, 255, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
    "navy": (0, 0, 128),
    "teal": (0, 128, 128),
    "orange": (255, 165, 0),
}
_DESC_ATTRS = ("id", "class", "src", "href", "name", "type", "for")


class _No:
    """Nó do DOM: o mínimo para casar seletor, ler texto e subir para o pai."""

    __slots__ = ("attrs", "children", "parent", "partes", "tag")

    def __init__(self, tag: str, attrs: dict, parent: _No | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_No] = []
        self.partes: list[str] = []


class _Arvore(HTMLParser):
    """HTML torto não é erro aqui: é o caso comum.

    O `--dump-dom` já devolve o DOM normalizado pelo Chrome, mas o fallback (GET
    cru) e o varredor de `dist_path` leem o arquivo como está. Tag sem fechamento,
    fechamento sem abertura e atributo sem valor entram todos por este caminho, e
    nenhum deles pode levantar exceção: o retorno é string de diagnóstico, e uma
    exceção aqui viraria "a página não existe" para o modelo.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raiz = _No("#document", {}, None)
        self.pilha = [self.raiz]
        self.css: list[str] = []
        self._em_style = False

    def handle_starttag(self, tag, attrs):
        no = _No(tag, {k.lower(): (v or "") for k, v in attrs}, self.pilha[-1])
        self.pilha[-1].children.append(no)
        if tag not in VOID_TAGS:
            self.pilha.append(no)
        if tag == "style":
            self._em_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self._em_style = False
        # Fecha até a abertura correspondente; fechamento órfão é ignorado em
        # vez de estourar a pilha (`</div>` a mais é o typo mais comum de todos).
        for i in range(len(self.pilha) - 1, 0, -1):
            if self.pilha[i].tag == tag:
                del self.pilha[i:]
                return

    def handle_data(self, data):
        if self._em_style:
            self.css.append(data)
        else:
            self.pilha[-1].partes.append(data)


def _parse_html(html: str) -> _Arvore:
    arvore = _Arvore()
    try:
        arvore.feed(html)
        arvore.close()
    except Exception:  # noqa: BLE001 - HTML de entrada não derruba a tool
        pass
    return arvore


def _walk(no: _No):
    for filho in no.children:
        yield filho
        yield from _walk(filho)


def _texto(no: _No) -> str:
    partes = list(no.partes)
    for d in _walk(no):
        partes.extend(d.partes)
    return " ".join(" ".join(partes).split())


def _desc(no: _No) -> str:
    """`<img src="logo.png">` — o elemento como o humano vai procurá-lo no arquivo."""
    marcas = "".join(
        f' {k}="{no.attrs[k][:40]}"' for k in _DESC_ATTRS if no.attrs.get(k)
    )
    return f"<{no.tag}{marcas}>"


# --------------------------------------------------------------------------- #
# seletor
# --------------------------------------------------------------------------- #


def parse_selector(sel: str) -> tuple[str | None, str | None, str | None] | None:
    """`(tag, id, classe)` das 4 formas simples, ou None.

    Sem descendência, sem `>`, sem `:nth-child`: um subconjunto honesto de CSS é
    melhor que um matcher meia-boca que casa o elemento errado calado.
    """
    s = (sel or "").strip()
    if re.fullmatch(r"[a-zA-Z][\w-]*", s):
        return (s.lower(), None, None)
    if re.fullmatch(r"#[\w-]+", s):
        return (None, s[1:], None)
    if re.fullmatch(r"\.[\w-]+", s):
        return (None, None, s[1:])
    m = re.fullmatch(r"([a-zA-Z][\w-]*)\.([\w-]+)", s)
    if m:
        return (m.group(1).lower(), None, m.group(2))
    return None


def _casa(no: _No, alvo: tuple[str | None, str | None, str | None]) -> bool:
    tag, ident, classe = alvo
    if tag and no.tag != tag:
        return False
    if ident and no.attrs.get("id") != ident:
        return False
    return not (classe and classe not in no.attrs.get("class", "").split())


# --------------------------------------------------------------------------- #
# CSS literal
# --------------------------------------------------------------------------- #


def _decls(bloco: str) -> dict:
    out = {}
    for parte in bloco.split(";"):
        prop, _, val = parte.partition(":")
        p = prop.strip().lower()
        if (p in CSS_PROPS or p == CSS_FUNDO) and val.strip():
            out[p] = val.strip()
    return out


def _regras(css: str) -> list[tuple[tuple, dict]]:
    """Regras do `<style>` cujo seletor é uma das formas simples.

    `@media`, `:hover` e descendência caem fora porque `parse_selector` os recusa
    — e cair fora é o certo: aplicá-los sem entender a condição daria um computed
    que o navegador não usou.
    """
    regras: list[tuple[tuple, dict]] = []
    limpo = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
    for bloco in limpo.split("}"):
        sel, chave, corpo = bloco.partition("{")
        if not chave:
            continue
        decls = _decls(corpo)
        if not decls:
            continue
        for parte in sel.split(","):
            alvo = parse_selector(parte)
            if alvo:
                regras.append((alvo, decls))
    return regras


def _computed(no: _No, regras: list[tuple[tuple, dict]]) -> dict:
    """Cascata pobre: regras na ordem do documento, `style=` por cima."""
    out: dict = {}
    for alvo, decls in regras:
        if _casa(no, alvo):
            out.update(decls)
    out.update(_decls(no.attrs.get("style", "")))
    return out


def parse_color(valor: str | None) -> tuple[int, int, int] | None:
    """RGB de um valor LITERAL. `inherit`, `currentColor` e `var()` -> None.

    None aqui significa "não sei", e quem chama tem que dizer isso em voz alta em
    vez de assumir preto no branco — a suposição silenciosa é o que faz auditoria
    de contraste reprovar página que está boa.
    """
    s = (valor or "").strip().lower()
    if s in _CORES:
        return _CORES[s]
    m = re.fullmatch(r"#([0-9a-f]{3})", s)
    if m:
        return tuple(int(c * 2, 16) for c in m.group(1))  # type: ignore[return-value]
    m = re.fullmatch(r"#([0-9a-f]{6})", s)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.fullmatch(r"rgba?\(([^)]*)\)", s)
    if m:
        nums = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()]
        if len(nums) == 4 and nums[3] not in ("1", "1.0", "100%"):
            return None  # translúcido: a cor final depende do que está atrás
        try:
            rgb = [round(float(n.rstrip("%")) * (2.55 if n.endswith("%") else 1)) for n in nums[:3]]
        except ValueError:
            return None
        if len(rgb) == 3 and all(0 <= v <= 255 for v in rgb):
            return (rgb[0], rgb[1], rgb[2])
    return None


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """Razão WCAG 2.x entre duas cores (1.0 a 21.0)."""

    def canal(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    def lum(c: tuple[int, int, int]) -> float:
        r, g, b = (canal(v) for v in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    claro, escuro = sorted((lum(fg), lum(bg)), reverse=True)
    return (claro + 0.05) / (escuro + 0.05)


def _fundo(no: _No, regras: list[tuple[tuple, dict]]) -> tuple[int, int, int] | None:
    """Sobe até achar um `background-color` literal. Nada -> None (não avaliável)."""
    atual: _No | None = no
    while atual is not None:
        cor = parse_color(_computed(atual, regras).get(CSS_FUNDO))
        if cor:
            return cor
        atual = atual.parent
    return None


# --------------------------------------------------------------------------- #
# obter o DOM
# --------------------------------------------------------------------------- #


def _cerca(ws: Path, port: int, tool: str) -> str | None:
    """A mesma cerca do `view_render`, com o nome da tool trocado na mensagem.

    Reusa `_cerca_porta` em vez de reescrever: a única diferença legítima entre as
    duas é quem apareceu no texto do recusado.
    """
    erro = _cerca_porta(ws, port)
    return None if erro is None else erro.replace("view_render", tool)


def dump_dom(url: str, timeout_s: float = DOM_TIMEOUT_S) -> tuple[str | None, str | None]:
    """`(html, erro)` do `--dump-dom`: o DOM DEPOIS de script e normalização."""
    from harness import uiverify

    exe = uiverify.chrome()
    if exe is None:
        return None, uiverify.MISSING_CHROME
    argv = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--dump-dom",
        url,
    ]
    try:
        # check=False: o returncode do Chrome não decide nada aqui — o sinal de
        # pronto é o stdout ter DOM, igual ao `screenshot` que olha o PNG e não o
        # código de saída.
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"dump-dom: {type(exc).__name__}: {exc}"
    if not proc.stdout.strip():
        return None, f"dump-dom: chrome saiu {proc.returncode} sem DOM"
    return proc.stdout, None


def fetch_dom(ws: Path, port: int, tool: str) -> tuple[str | None, str, str | None]:
    """`(html, fonte, erro)`. Chrome primeiro; GET cru quando o Chrome falha.

    O fallback não é luxo: sem Chrome na máquina (CI, container magro) a tool
    inteira morreria, e o HTML servido ainda responde a maior parte das perguntas.
    Ele vem etiquetado como `fonte` porque a diferença importa — script que
    injeta nó não aparece no GET.
    """
    erro = _cerca(ws, port, tool)
    if erro:
        return None, "", erro

    html, falha = dump_dom(f"http://{LOOPBACK}:{port}/")
    if html is not None:
        return html, "chrome --dump-dom", None

    bruto = local_probe(ws, port)
    cabeca, sep, corpo = bruto.partition("\n\n")
    if not sep or "-> 200" not in cabeca:
        return None, "", f"{falha}; GET cru também não trouxe HTML: {cabeca.strip() or bruto[:200]}"
    return corpo, f"GET cru (sem Chrome: {falha})", None


# --------------------------------------------------------------------------- #
# inspect_dom
# --------------------------------------------------------------------------- #


def inspect_dom(ws: str | Path, port: int, selector: str) -> str:
    """Um elemento do DOM: existe, texto, atributos e o computed que dá para provar."""
    alvo = parse_selector(selector)
    if alvo is None:
        return (
            f"inspect_dom: {NAO_SUPORTADO} — tag, #id, .class ou tag.class "
            f"(recebido {selector!r}). Combinação, descendência e pseudo-classe não "
            "são interpretadas aqui."
        )

    html, fonte, erro = fetch_dom(Path(ws), _as_int(port), "inspect_dom")
    if erro:
        return f"inspect_dom: {erro}"
    assert html is not None

    arvore = _parse_html(html)
    regras = _regras("\n".join(arvore.css))
    achados = [no for no in _walk(arvore.raiz) if _casa(no, alvo)]

    linhas = [f"inspect_dom {selector} em http://{LOOPBACK}:{_as_int(port)}/ ({fonte})"]
    if not achados:
        linhas.append("existe: não")
        linhas.append(
            "Nenhum elemento casou. Confira o seletor com o HTML que você escreveu — "
            "e lembre que o DOM aqui é o do navegador, não o do arquivo."
        )
        return "\n".join(linhas)

    no = achados[0]
    quantos = f"sim ({len(achados)} encontrados, mostrando o 1º)" if len(achados) > 1 else "sim"
    texto = _texto(no)
    computed = _computed(no, regras)
    linhas += [
        f"existe: {quantos}",
        f"tag: {no.tag}",
        "atributos: " + (" ".join(f'{k}="{v}"' for k, v in no.attrs.items()) or "(nenhum)"),
        f"texto ({MAX_TEXT} chars): " + ((texto[:MAX_TEXT] + "…") if len(texto) > MAX_TEXT else texto or "(vazio)"),
        "computed (parcial — só style= inline e regras literais de <style>): "
        + ("; ".join(f"{p}={computed[p]}" for p in CSS_PROPS if p in computed) or "(nada declarado literalmente)"),
        BBOX,
    ]
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# a11y_audit
# --------------------------------------------------------------------------- #


def _labels_por_for(arvore: _Arvore) -> set[str]:
    return {
        no.attrs["for"]
        for no in _walk(arvore.raiz)
        if no.tag == "label" and no.attrs.get("for")
    }


def _tem_ancestral(no: _No, tag: str) -> bool:
    atual = no.parent
    while atual is not None:
        if atual.tag == tag:
            return True
        atual = atual.parent
    return False


def _varre(rotulo: str, arvore: _Arvore) -> tuple[list[str], list[str]]:
    """`(achados, nao_avaliaveis)` de um documento.

    Achado é sempre "o que está errado + como consertar": relatório de a11y que
    só aponta o defeito faz o modelo inventar o conserto.
    """
    achados: list[str] = []
    nao_avaliaveis: list[str] = []
    regras = _regras("\n".join(arvore.css))
    fors = _labels_por_for(arvore)
    nivel_anterior = 0

    for no in _walk(arvore.raiz):
        if no.tag == "img" and "alt" not in no.attrs:
            achados.append(
                f"{rotulo}/{_desc(no)} — img sem alt: adicione alt=\"descrição do "
                'conteúdo\", ou alt="" se a imagem for puramente decorativa'
            )

        if no.tag == "input" and no.attrs.get("type", "text").lower() not in TIPOS_SEM_LABEL:
            tem_rotulo = (
                any(no.attrs.get(a) for a in ROTULO_ATTRS)
                or (no.attrs.get("id") in fors)
                or _tem_ancestral(no, "label")
            )
            if not tem_rotulo:
                ident = no.attrs.get("id")
                conserto = (
                    f'<label for="{ident}">…</label>' if ident else 'envolva num <label> ou dê um id e use <label for="…">'
                )
                achados.append(
                    f"{rotulo}/{_desc(no)} — input sem label associado: {conserto} "
                    "(ou aria-label se o rótulo visível não existir)"
                )

        if re.fullmatch(r"h[1-6]", no.tag):
            nivel = int(no.tag[1])
            if nivel_anterior and nivel > nivel_anterior + 1:
                achados.append(
                    f"{rotulo}/{_desc(no)} — heading fora de ordem: <{no.tag}> depois de "
                    f"<h{nivel_anterior}> pula h{nivel_anterior + 1}; use <h{nivel_anterior + 1}> "
                    "ou reorganize a hierarquia"
                )
            nivel_anterior = nivel

        if no.tag == "a":
            tem_rotulo = any(no.attrs.get(a) for a in ROTULO_ATTRS)
            tem_img = any(d.tag == "img" and d.attrs.get("alt") for d in _walk(no))
            if not _texto(no) and not tem_rotulo and not tem_img:
                achados.append(
                    f"{rotulo}/{_desc(no)} — link sem texto acessível (vazio ou só ícone): "
                    'adicione aria-label="ação do link", ou alt no <img> de dentro'
                )

        cor = parse_color(_computed(no, regras).get("color"))
        if cor and _texto(no):
            fundo = _fundo(no, regras)
            if fundo is None:
                nao_avaliaveis.append(f"{rotulo}/{_desc(no)}")
            else:
                razao = contrast_ratio(cor, fundo)
                if razao < WCAG_AA:
                    achados.append(
                        f"{rotulo}/{_desc(no)} — contraste {razao:.2f}:1 abaixo de "
                        f"{WCAG_AA}:1 (WCAG AA, texto normal): escureça o texto ou clareie o "
                        "fundo até a razão passar"
                    )

    return achados, nao_avaliaveis


def a11y_audit(ws: str | Path, port: int | None = None, dist_path: str | None = None) -> str:
    """Auditoria de acessibilidade do DOM, sem lighthouse e sem chute.

    `port` audita a página servida (DOM do navegador); `dist_path` varre os
    `.html` do diretório, e é por isso que o achado sai com nome de arquivo — é o
    arquivo que o modelo vai abrir para consertar.
    """
    base = Path(ws)
    if (port is None) == (dist_path is None):
        return "a11y_audit exige exatamente um de port= ou dist_path="

    docs: list[tuple[str, _Arvore]] = []
    if port is not None:
        html, fonte, erro = fetch_dom(base, _as_int(port), "a11y_audit")
        if erro:
            return f"a11y_audit: {erro}"
        assert html is not None
        alvo = f"http://{LOOPBACK}:{_as_int(port)}/ ({fonte})"
        docs.append((f"port-{_as_int(port)}", _parse_html(html)))
    else:
        root = (base / dist_path) if not Path(dist_path).is_absolute() else Path(dist_path)
        if not root.is_dir():
            return f"a11y_audit recusado: {dist_path} não é um diretório"
        arquivos = sorted(root.rglob("*.html"))[:MAX_A11Y_FILES]
        if not arquivos:
            return f"a11y_audit: nenhum .html em {dist_path}"
        alvo = f"{dist_path} ({len(arquivos)} arquivo(s))"
        for arq in arquivos:
            try:
                texto = arq.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return f"a11y_audit falhou lendo {arq.name}: {type(exc).__name__}: {exc}"
            docs.append((str(arq.relative_to(root)), _parse_html(texto)))

    achados: list[str] = []
    nao_avaliaveis: list[str] = []
    for rotulo, arvore in docs:
        a, n = _varre(rotulo, arvore)
        achados += a
        nao_avaliaveis += n

    linhas = [f"a11y_audit em {alvo}"]
    linhas += [f"achado: {a}" for a in achados] or ["nenhum achado."]
    if nao_avaliaveis:
        # Não é reprovação: é o limite da tool declarado. Cor que vem de arquivo
        # .css externo, de var() ou de herança não está no alcance deste parser.
        linhas.append(
            f"contraste: {NAO_AVALIAVEL} em {len(nao_avaliaveis)} elemento(s) — "
            "cor ou fundo não são literais no style=/<style> (CSS externo, var() "
            "ou herança). Isso não conta como achado."
        )
    plural = "achado" if len(achados) == 1 else "achados"
    linhas.append(f"{len(achados)} {plural}")
    return "\n".join(linhas)


def make_dom_tools(ws: str | Path) -> list:
    """Tools de inspeção de DOM com o workspace fixado.

    Separada de `make_view_tools` porque olhar a tela e ler o DOM são montagens
    independentes: uma máquina sem modelo de visão ainda quer as duas daqui.
    """
    from langchain_core.tools import StructuredTool  # lazy: LangChain é extra

    base = Path(ws)

    def _inspect_dom(selector: str, port: int) -> str:
        """Inspeciona um elemento no DOM montado pelo navegador."""
        try:
            return inspect_dom(base, port, selector)
        except Exception as exc:  # tool node não pode receber exceção
            return f"inspect_dom falhou: {type(exc).__name__}: {exc}"

    def _a11y_audit(port: int | None = None, dist_path: str | None = None) -> str:
        """Audita acessibilidade do DOM e diz como consertar cada achado."""
        try:
            return a11y_audit(base, port=port, dist_path=dist_path)
        except Exception as exc:
            return f"a11y_audit falhou: {type(exc).__name__}: {exc}"

    return [
        StructuredTool.from_function(
            func=_inspect_dom,
            name="inspect_dom",
            description=(
                "Mostra UM elemento do DOM que o navegador montou: se existe, o texto, "
                "os atributos e o computed que dá para provar. Use quando o screenshot "
                "mostrou o problema e você precisa apontar o elemento, ou para saber se "
                "o nó que o script deveria injetar chegou lá. selector aceita só forma "
                "simples: tag, #id, .class ou tag.class. port é a porta do start_server "
                "desta run. bbox e computed completo exigem CDP e não vêm."
            ),
        ),
        StructuredTool.from_function(
            func=_a11y_audit,
            name="a11y_audit",
            description=(
                "Audita acessibilidade do DOM: img sem alt, input sem label, headings "
                "fora de ordem, link só de ícone sem aria-label e contraste WCAG quando "
                "as cores são literais. Cada achado vem com o conserto. Passe "
                "port=<porta do start_server> ou dist_path=<dir com .html> — "
                "exatamente um. Contraste que depende de CSS externo sai como "
                "'não avaliável' e não conta como achado."
            ),
        ),
    ]


__all__ = [
    "BBOX",
    "MIN_SHOT_KB",
    "NAO_AVALIAVEL",
    "NAO_SUPORTADO",
    "a11y_audit",
    "contrast_ratio",
    "dump_dom",
    "inspect_dom",
    "make_dom_tools",
    "make_view_tools",
    "parse_color",
    "parse_selector",
    "render",
]
