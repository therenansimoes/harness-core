"""Testes do scaffold e do asset_gen.

Dois eixos. O primeiro é a borda da tool (kind inválido, destino ocupado,
traversal): tudo tem que RECUSAR e, no caso do destino ocupado, recusar sem
tocar no disco. O segundo é o conteúdo dos templates — a parte que costuma
apodrecer: aqui o teste roda o pytest do próprio template de FastAPI (marcado
`slow`, instala) e passa o HTML do static-site por um parser de verdade para
provar que os landmarks e o skip-link continuam lá.
"""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

from harness import scaffold as sc

CATALOG = sc.load_catalog()
KINDS = sorted(CATALOG)

# Teto de curadoria: template é ponto de partida, não framework.
MAX_FILES = 8


# --------------------------------------------------------------------------- bordas


def test_kind_invalido_lista_os_validos(tmp_path):
    with pytest.raises(sc._Refused) as exc:
        sc.scaffold("next-turbo-mega", "x", tmp_path)
    msg = str(exc.value)
    assert "kind desconhecido" in msg
    for kind in KINDS:
        assert kind in msg  # erro claro = erro que já diz o que usar
    assert not list(tmp_path.iterdir())


def test_destino_ocupado_recusa_sem_escrever(tmp_path):
    alvo = tmp_path / "site"
    alvo.mkdir()
    (alvo / "meu.txt").write_text("trabalho anterior")

    with pytest.raises(sc._Refused, match="já tem conteúdo"):
        sc.scaffold("static-site", "site", tmp_path)

    assert [p.name for p in alvo.iterdir()] == ["meu.txt"]
    assert (alvo / "meu.txt").read_text() == "trabalho anterior"


@pytest.mark.parametrize(
    "name",
    ["../fora", "..", ".", "sub/dir", "/etc/passwd", "", ".oculto", "a/../../b"],
)
def test_path_traversal_rejeitado(tmp_path, name):
    with pytest.raises(sc._Refused):
        sc.scaffold("static-site", name, tmp_path)
    assert not list(tmp_path.iterdir())
    assert not (tmp_path.parent / "fora").exists()


def test_tools_devolvem_string_em_vez_de_excecao(tmp_path):
    pytest.importorskip("langchain_core", reason="extra deepagents não instalado")
    tools = {t.name: t for t in sc.make_scaffold_tools(tmp_path)}
    assert set(tools) == {"scaffold", "asset_gen"}
    assert "recusado" in tools["scaffold"].invoke({"kind": "nada", "name": "x"})
    assert "recusado" in tools["asset_gen"].invoke({"kind": "nada", "spec": "x"})
    ok = tools["scaffold"].invoke({"kind": "static-site", "name": "site"})
    assert ok.startswith("scaffold ok")


# --------------------------------------------------------------------------- catálogo


def test_catalogo_tem_os_tres_kinds():
    assert KINDS == ["fastapi-min", "static-site", "vite-vanilla"]
    for kind, entry in CATALOG.items():
        assert (sc.TEMPLATES_ROOT / entry["dir"]).is_dir(), kind
        assert entry.get("summary") and entry.get("use"), kind
        assert entry.get("next"), kind


@pytest.mark.parametrize("kind", KINDS)
def test_template_cabe_no_teto_de_arquivos(tmp_path, kind):
    out = sc.scaffold(kind, "proj", tmp_path)
    arquivos = sorted(p.relative_to(tmp_path / "proj").as_posix() for p in (tmp_path / "proj").rglob("*") if p.is_file())
    assert arquivos, kind
    assert len(arquivos) <= MAX_FILES, f"{kind}: {len(arquivos)} arquivos, teto {MAX_FILES}"
    for f in arquivos:
        assert f in out  # a saída lista o que criou
    for cmd in CATALOG[kind]["next"]:
        assert cmd.split()[0] in out


# --------------------------------------------------------------------------- static-site


_VOID = frozenset(
    {"meta", "link", "br", "img", "input", "hr", "source", "area", "base", "col"}
)


class _Colhedor(HTMLParser):
    """Parser stdlib: coleta tags e atributos, e acusa tag mal fechada."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: dict[str, list[dict]] = {}
        self.pilha: list[str] = []
        self.desbalanceadas: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.setdefault(tag, []).append(dict(attrs))
        if tag not in _VOID:
            self.pilha.append(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if self.pilha and self.pilha[-1] == tag:
            self.pilha.pop()
        else:
            self.desbalanceadas.append(tag)


def _parse(html: str) -> _Colhedor:
    p = _Colhedor()
    p.feed(html)
    p.close()
    return p


@pytest.mark.parametrize("kind", ["static-site", "vite-vanilla"])
def test_html_do_template_tem_landmarks_e_acessibilidade(tmp_path, kind):
    sc.scaffold(kind, "site", tmp_path)
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    dom = _parse(html)

    assert dom.desbalanceadas == [], f"{kind}: tag fechada fora de ordem"
    assert dom.pilha == [], f"{kind}: tag aberta sem fechar: {dom.pilha}"

    # Landmarks: sem isto o leitor de tela não tem por onde navegar.
    for landmark in ("html", "head", "body", "header", "main", "footer"):
        assert landmark in dom.tags, f"{kind}: falta <{landmark}>"

    assert dom.attrs["html"][0].get("lang"), f"{kind}: <html> sem lang"

    viewport = [m for m in dom.attrs["meta"] if m.get("name") == "viewport"]
    assert viewport and "width=device-width" in viewport[0]["content"], f"{kind}: sem viewport"
    assert any(m.get("charset") for m in dom.attrs["meta"]), f"{kind}: sem charset"
    assert any(m.get("name") == "description" for m in dom.attrs["meta"])

    # Skip-link: primeiro <a> da página e apontando para o id do <main>.
    main_id = dom.attrs["main"][0].get("id")
    assert main_id, f"{kind}: <main> sem id para o skip-link"
    primeiro_a = dom.attrs["a"][0]
    assert "skip-link" in primeiro_a.get("class", "")
    assert primeiro_a.get("href") == f"#{main_id}"

    assert "<title>" in html and "placeholder" in html.lower()


def test_tokens_cobrem_escala_e_os_dois_temas():
    tokens = (sc.TEMPLATES_ROOT / "static-site" / "tokens.css").read_text(encoding="utf-8")
    # Escala de espaçamento 4/8/16/24/40 px em rem.
    for var, valor in (
        ("--space-1", "0.25rem"),
        ("--space-2", "0.5rem"),
        ("--space-3", "1rem"),
        ("--space-4", "1.5rem"),
        ("--space-5", "2.5rem"),
    ):
        assert f"{var}: {valor}" in tokens
    for var in ("--text-xs", "--text-md", "--text-2xl", "--text-display", "--measure"):
        assert var in tokens
    # Tema: preferência do SO E override manual.
    assert "prefers-color-scheme: dark" in tokens
    assert '[data-theme="dark"]' in tokens and '[data-theme="light"]' in tokens
    assert "color-scheme: light dark" in tokens


# --------------------------------------------------------------------------- assets


@pytest.mark.parametrize("shape", sc.ICON_SHAPES)
def test_icone_gera_svg_que_parseia(tmp_path, shape):
    out = sc.asset_gen("icon", shape, tmp_path)
    path = tmp_path / "assets" / f"icon-{shape}.svg"
    assert path.is_file() and path.name in out
    raiz = ET.fromstring(path.read_text(encoding="utf-8"))
    assert raiz.tag == "{http://www.w3.org/2000/svg}svg"
    assert raiz.get("viewBox") == "0 0 24 24"
    assert len(list(raiz)) >= 2  # <title> + geometria


def test_forma_de_icone_desconhecida_recusa_sem_escrever(tmp_path):
    with pytest.raises(sc._Refused, match="forma desconhecida"):
        sc.asset_gen("icon", "foguete", tmp_path)
    assert not (tmp_path / "assets").exists()


def test_placeholder_le_dimensao_e_rotulo(tmp_path):
    sc.asset_gen("placeholder", "1200x630 Hero da home", tmp_path)
    path = tmp_path / "assets" / "placeholder-hero-da-home-1200x630.svg"
    raiz = ET.fromstring(path.read_text(encoding="utf-8"))
    assert raiz.get("width") == "1200" and raiz.get("height") == "630"
    textos = [e.text for e in raiz.iter() if e.text]
    assert any("Hero da home" in t for t in textos)
    assert any("1200×630" in t for t in textos)


def test_placeholder_sem_dimensao_usa_default(tmp_path):
    sc.asset_gen("placeholder", "banner", tmp_path)
    w, h = sc.DEFAULT_PLACEHOLDER
    raiz = ET.fromstring((tmp_path / "assets" / f"placeholder-banner-{w}x{h}.svg").read_text())
    assert raiz.get("viewBox") == f"0 0 {w} {h}"


@pytest.mark.parametrize("spec,esperado", [("Oficina Aruã", "OA"), ("Harness hex", "H")])
def test_logo_mark_monograma(tmp_path, spec, esperado):
    sc.asset_gen("logo-mark", spec, tmp_path)
    svgs = list((tmp_path / "assets").glob("logo-*.svg"))
    assert len(svgs) == 1
    raiz = ET.fromstring(svgs[0].read_text(encoding="utf-8"))
    assert raiz.get("aria-label") == esperado
    if "hex" in spec:
        assert raiz.find("{http://www.w3.org/2000/svg}polygon") is not None
    else:
        assert raiz.find("{http://www.w3.org/2000/svg}circle") is not None


def test_rotulo_hostil_nao_quebra_o_xml(tmp_path):
    sc.asset_gen("placeholder", '400x300 <script>&"fim', tmp_path)
    svg = next((tmp_path / "assets").glob("*.svg")).read_text(encoding="utf-8")
    ET.fromstring(svg)  # a garantia central: sempre XML válido
    assert "<script>" not in svg


def test_asset_nao_sobrescreve(tmp_path):
    sc.asset_gen("icon", "check", tmp_path)
    sc.asset_gen("icon", "check", tmp_path)
    nomes = sorted(p.name for p in (tmp_path / "assets").glob("*.svg"))
    assert nomes == ["icon-check-2.svg", "icon-check.svg"]


def test_paleta_sai_do_tokens_do_workspace(tmp_path):
    # Sem tokens: default.
    assert sc.palette(tmp_path)["accent"] == sc.DEFAULT_PALETTE["accent"]

    (tmp_path / "tokens.css").write_text(
        ":root{--hue:12;--accent:light-dark(oklch(0.5 0.2 var(--hue)),oklch(0.8 0.1 var(--hue)));"
        "--surface:#fafafa;--text:#111;--border:#eee;}",
        encoding="utf-8",
    )
    pal = sc.palette(tmp_path)
    # light-dark() não resolve em SVG solto: fica o lado claro, com a hue literal.
    assert pal["accent"] == "oklch(0.5 0.2 12)"
    assert pal["surface"] == "#fafafa"
    assert sc.DEFAULT_PALETTE["accent"] not in sc.asset_gen("icon", "casa", tmp_path)
    svg = (tmp_path / "assets" / "icon-casa.svg").read_text(encoding="utf-8")
    assert 'stroke="oklch(0.5 0.2 12)"' in svg
    ET.fromstring(svg)


def test_paleta_do_template_real_gera_svg_valido(tmp_path):
    sc.scaffold("static-site", "site", tmp_path)
    # tokens.css do template dentro do próprio projeto criado.
    pal = sc.palette(tmp_path / "site")
    assert pal["accent"].startswith("oklch(") and "var(" not in pal["accent"]
    sc.asset_gen("logo-mark", "Projeto Novo", tmp_path / "site")
    ET.fromstring((tmp_path / "site" / "assets" / "logo-pn-circulo.svg").read_text())


# --------------------------------------------------------------------------- template roda (slow)


@pytest.mark.slow
def test_fastapi_min_passa_o_proprio_pytest(tmp_path):
    """Rede + instalação: `-m slow`. Prova que o template ENTREGA teste verde,
    não só arquivo bonito."""
    if shutil.which("uv") is None:
        pytest.skip("uv não está no PATH")
    sc.scaffold("fastapi-min", "api", tmp_path)
    proc = subprocess.run(
        ["uv", "run", "pytest", "-q"],
        cwd=tmp_path / "api",
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "2 passed" in proc.stdout


@pytest.mark.slow
def test_vite_vanilla_passa_o_proprio_vitest(tmp_path):
    """Rede + npm install: `-m slow`."""
    if shutil.which("npm") is None:
        pytest.skip("npm não está no PATH")
    sc.scaffold("vite-vanilla", "app", tmp_path)
    cwd = tmp_path / "app"
    inst = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=cwd,
                          capture_output=True, text=True, check=False, timeout=900)
    assert inst.returncode == 0, inst.stderr[-2000:]
    proc = subprocess.run(
        ["npm", "test"], cwd=cwd, capture_output=True, text=True, check=False, timeout=600
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}"


def test_fastapi_min_e_importavel_com_o_fastapi_do_repo(tmp_path):
    """Sem instalar nada: se `fastapi` já existe no ambiente, o template roda
    aqui mesmo. É o teste rápido que cobre o que o `slow` cobre devagar."""
    pytest.importorskip("fastapi", reason="fastapi não está no ambiente do repo")
    pytest.importorskip("httpx", reason="TestClient precisa de httpx")
    sc.scaffold("fastapi-min", "api", tmp_path)
    proc = subprocess.run(
        ["python", "-m", "pytest", "-q"],
        cwd=tmp_path / "api",
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def test_scaffold_ignora_lixo_de_cache(tmp_path):
    lixo = sc.TEMPLATES_ROOT / "fastapi-min" / "app" / "__pycache__"
    lixo.mkdir(exist_ok=True)
    (lixo / "main.cpython-311.pyc").write_bytes(b"\x00")
    try:
        sc.scaffold("fastapi-min", "api", tmp_path)
        assert not list((tmp_path / "api").rglob("__pycache__"))
        assert not list((tmp_path / "api").rglob("*.pyc"))
    finally:
        shutil.rmtree(lixo, ignore_errors=True)


def test_catalog_manual_lista_uma_linha_por_kind():
    linhas = sc.catalog_manual().splitlines()
    assert len(linhas) == len(KINDS)
    assert all(linha.startswith("- ") for linha in linhas)


def test_templates_root_aponta_para_o_repo():
    assert sc.TEMPLATES_ROOT.is_dir()
    assert (sc.TEMPLATES_ROOT / "catalog.toml").is_file()
    assert Path(sc.__file__).parent.name == "harness"
