"""`inspect_dom` / `a11y_audit`: o que esta suíte protege é o SILÊNCIO CORRETO.

Uma auditoria de acessibilidade que inventa achado é pior que nenhuma: o modelo
gasta o turno "consertando" markup que já estava certo, e o achado real fica
enterrado no meio do ruído. Por isso o teste central não é "achou as três
violações" — é "achou as três E nenhuma a mais", com o dobro de markup correto
na mesma página para o parser errar se quiser.

O mesmo vale para contraste: cor que vem de CSS externo ou de `var()` é `não
avaliável`, nunca reprovação. E vale para o parser: HTML torto é o caso comum, e
exceção aqui viraria "a página não existe" para o modelo.

Chrome é fake (o CI roda ubuntu sem Chrome): um script que escreve no stdout o
arquivo apontado por `HARNESS_FAKE_DOM`, que é exatamente o contrato do
`--dump-dom`. Nenhum token é gasto.
"""

import json
import os
import stat
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness import uiverify
from harness.backends import dom_tools, procs

FAKE_CHROME_DOM = '''#!{python}
import os, sys
sys.stdout.write(open(os.environ["HARNESS_FAKE_DOM"], encoding="utf-8").read())
'''

FAKE_CHROME_MUDO = '''#!{python}
import sys
sys.exit(1)
'''

# Três violações, e o dobro de markup correto em volta para o parser ter chance de
# errar: img com alt, input com <label for>, input dentro do <label>, input
# hidden (não precisa de rótulo), link com aria-label e link com texto.
PAGINA_3_VIOLACOES = """<!doctype html>
<html lang="pt-br"><head><title>ok</title></head><body>
  <h1>Título</h1>
  <h3>Pulou o h2</h3>

  <img src="sem.png">
  <img src="com.png" alt="logo da empresa">

  <input type="text" id="nome">
  <label for="email">Email</label><input type="email" id="email">
  <label>Busca <input type="search" name="q"></label>
  <input type="hidden" name="csrf" value="abc">
  <input type="submit" value="Enviar">

  <a href="/fechar" aria-label="Fechar o painel"><i class="icon-x"></i></a>
  <a href="/sobre">Sobre nós</a>
  <a href="/home"><img src="casa.png" alt="Início"></a>
</body></html>
"""

PAGINA_TORTA = """<!doctype html>
<html><body>
  <div class="card"><p>texto aberto</div></p>
  </section>
  <ul><li>um<li>dois</ul>
  <img src=x alt=y>
  <input disabled>
  <span class="card">fim
  <style>.card { color: #333 </style>
</body>
"""


@pytest.fixture
def fake_chrome(tmp_path, monkeypatch):
    """Chrome que faz `--dump-dom` do arquivo em HARNESS_FAKE_DOM."""
    exe = tmp_path / "fake-chrome-dom"
    exe.write_text(FAKE_CHROME_DOM.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(uiverify.CHROME_ENV, str(exe))
    return exe


@pytest.fixture
def servido(tmp_path, monkeypatch):
    """`(ws, port)` com a porta registrada em procs.json — a cerca do local_probe.

    `pid` é o do próprio pytest para o registro estar vivo de verdade: fingir
    `_vivo` seria testar o mock em vez da cerca.
    """

    def _servido(html: str) -> tuple:
        ws = tmp_path / "ws"
        (ws / ".harness").mkdir(parents=True)
        port = procs.alloc_port()
        procs.procs_path(ws).write_text(
            json.dumps([{"id": "srv", "pid": os.getpid(), "pgid": os.getpid(), "port": port}]),
            encoding="utf-8",
        )
        dom = tmp_path / "dump.html"
        dom.write_text(html, encoding="utf-8")
        monkeypatch.setenv("HARNESS_FAKE_DOM", str(dom))
        return ws, port

    return _servido


# --------------------------------------------------------------------------- a11y


def test_a11y_acha_as_tres_violacoes_e_nenhuma_a_mais(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(PAGINA_3_VIOLACOES, encoding="utf-8")

    out = dom_tools.a11y_audit(tmp_path, dist_path="dist")

    achados = [l for l in out.splitlines() if l.startswith("achado:")]
    assert len(achados) == 3, out
    assert "3 achados" in out
    # Cada achado é de uma classe diferente e aponta o elemento errado, não o certo.
    assert any("img sem alt" in a and "sem.png" in a for a in achados), out
    assert any("input sem label" in a and 'id="nome"' in a for a in achados), out
    assert any("heading fora de ordem" in a and "<h3" in a for a in achados), out
    # Zero falso positivo: nada do markup correto aparece na lista.
    for correto in ("com.png", 'id="email"', 'name="q"', "csrf", "Fechar", "/sobre", "casa.png"):
        assert correto not in out, f"{correto} é markup correto e virou achado:\n{out}"


def test_a11y_traz_o_conserto_junto_do_achado(tmp_path):
    """Achado sem conserto faz o modelo inventar o conserto."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(PAGINA_3_VIOLACOES, encoding="utf-8")

    out = dom_tools.a11y_audit(tmp_path, dist_path="dist")

    assert 'alt="descrição' in out
    assert '<label for="nome">' in out
    assert "index.html/" in out  # o achado diz o arquivo para abrir


def test_contraste_nao_resolvivel_nao_reprova(tmp_path):
    """Cor literal e fundo em CSS externo: `não avaliável`, e ZERO achados.

    Assumir fundo branco aqui é o jeito clássico de reprovar página que está boa.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="app.css"></head>'
        '<body><p style="color:#767676">texto</p>'
        '<span style="color:var(--fg)">outro</span></body></html>',
        encoding="utf-8",
    )

    out = dom_tools.a11y_audit(tmp_path, dist_path="dist")

    assert dom_tools.NAO_AVALIAVEL in out
    assert "não conta como achado" in out
    assert "0 achados" in out
    assert "contraste " not in out  # nenhuma razão foi inventada


def test_contraste_reprova_quando_os_dois_lados_sao_literais(tmp_path):
    """O outro lado da moeda: com fg E bg literais, a razão WCAG sai de verdade."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<html><head><style>body { background-color: #ffffff }"
        ".fraco { color: #cccccc }</style></head>"
        '<body><p class="fraco">quase invisível</p></body></html>',
        encoding="utf-8",
    )

    out = dom_tools.a11y_audit(tmp_path, dist_path="dist")

    assert "1 achado" in out
    assert "contraste 1.6" in out  # #ccc sobre #fff = 1.61:1
    assert "abaixo de 4.5:1" in out


def test_contrast_ratio_bate_com_a_wcag():
    """Preto no branco é 21:1; #767676 no branco é o limite 4.54:1 do AA.

    `#767676` é o cinza mais escuro que ainda PASSA em texto normal: é o número que
    prova que a curva de luminância está certa, não só a fórmula da razão.
    """
    assert dom_tools.contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)
    assert dom_tools.contrast_ratio((0x76, 0x76, 0x76), (255, 255, 255)) == pytest.approx(4.54, abs=0.02)
    assert dom_tools.contrast_ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0, abs=0.01)


def test_parse_color_diz_nao_sei_em_vez_de_chutar():
    assert dom_tools.parse_color("#fff") == (255, 255, 255)
    assert dom_tools.parse_color("rgb(10, 20, 30)") == (10, 20, 30)
    assert dom_tools.parse_color("white") == (255, 255, 255)
    for opaco in ("inherit", "currentColor", "var(--fg)", "rgba(0,0,0,0.4)", "", None):
        assert dom_tools.parse_color(opaco) is None, opaco


def test_a11y_exige_exatamente_um_alvo(tmp_path):
    assert "exatamente um" in dom_tools.a11y_audit(tmp_path)
    assert "exatamente um" in dom_tools.a11y_audit(tmp_path, port=1, dist_path="dist")


# --------------------------------------------------------------------------- inspect_dom


def test_seletor_inexistente_responde_existe_nao(fake_chrome, servido):
    ws, port = servido(PAGINA_3_VIOLACOES)

    out = dom_tools.inspect_dom(ws, port, "#nao-existe")

    assert "existe: não" in out
    assert "existe: sim" not in out


def test_seletor_complexo_recusado_sem_chamar_o_navegador(tmp_path):
    """Recusa ANTES do Chrome: seletor errado não custa um processo."""
    for complexo in ("div > p", "ul li", "a:hover", "#app .card", "div#app", "[data-x]"):
        out = dom_tools.inspect_dom(tmp_path, 1, complexo)
        assert dom_tools.NAO_SUPORTADO in out, complexo
        assert "tag.class" in out


def test_seletor_simples_aceito_nas_quatro_formas():
    assert dom_tools.parse_selector("div") == ("div", None, None)
    assert dom_tools.parse_selector("#app") == (None, "app", None)
    assert dom_tools.parse_selector(".card") == (None, None, "card")
    assert dom_tools.parse_selector("button.primary") == ("button", None, "primary")
    assert dom_tools.parse_selector("div > p") is None


def test_inspect_dom_devolve_texto_atributos_e_computed_parcial(fake_chrome, servido):
    ws, port = servido(
        "<html><head><style>.card { display: flex; color: #333; font-size: 14px }"
        "@media (max-width: 5px) { .card { display: none } }</style></head>"
        '<body><div class="card" id="app" style="color:#000">'
        "Olá <b>mundo</b></div></body></html>"
    )

    out = dom_tools.inspect_dom(ws, port, "div.card")

    assert "existe: sim" in out
    assert "tag: div" in out
    assert 'id="app"' in out
    assert "Olá mundo" in out
    assert "display=flex" in out
    assert "font-size=14px" in out
    assert "color=#000" in out  # style= inline ganha da regra do <style>
    assert dom_tools.BBOX in out
    assert "background-color" not in out  # lido para contraste, não exposto aqui


def test_inspect_dom_conta_quantos_casaram(fake_chrome, servido):
    ws, port = servido('<html><body><p class="x">a</p><p class="x">b</p></body></html>')

    out = dom_tools.inspect_dom(ws, port, "p.x")

    assert "2 encontrados, mostrando o 1º" in out


def test_inspect_dom_respeita_a_cerca_da_porta(tmp_path, fake_chrome):
    """Porta que esta run não subiu é recusada, e a mensagem diz a tool certa."""
    ws = tmp_path / "ws"
    ws.mkdir()

    out = dom_tools.inspect_dom(ws, 54321, "#app")

    assert "inspect_dom recusado" in out
    assert "view_render" not in out
    assert "start_server" in out


def test_texto_truncado_em_300_chars(fake_chrome, servido):
    ws, port = servido(f'<html><body><p id="l">{"x" * 900}</p></body></html>')

    out = dom_tools.inspect_dom(ws, port, "#l")

    assert "x" * 300 + "…" in out
    assert "x" * 301 not in out


# --------------------------------------------------------------------------- parser


def test_parser_nao_explode_com_html_torto(fake_chrome, servido):
    """`</div></p>` cruzado, `<li>` sem fechar, `</section>` órfão, `<style>` aberto.

    Nada aqui pode levantar exceção: o retorno da tool é string, e uma exceção
    viraria "a página não existe" para o modelo.
    """
    ws, port = servido(PAGINA_TORTA)

    out = dom_tools.inspect_dom(ws, port, ".card")

    assert "existe: sim" in out
    assert "2 encontrados" in out  # o div.card e o span.card
    assert "texto aberto" in out
    assert "falhou" not in out


def test_a11y_nao_explode_com_html_torto(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "torto.html").write_text(PAGINA_TORTA, encoding="utf-8")

    out = dom_tools.a11y_audit(tmp_path, dist_path="dist")

    # `<input disabled>` sem rótulo é achado legítimo; o resto do markup torto não.
    assert "achado:" in out
    assert "1 achado" in out
    assert "falhou" not in out


def test_regras_ignoram_seletor_que_nao_entende():
    """@media e descendência ficam fora do computed em vez de entrar errado."""
    regras = dom_tools._regras(
        ".a { color: red } @media print { .b { color: blue } } div p { color: green }"
    )

    alvos = [alvo for alvo, _ in regras]
    assert (None, None, "a") in alvos
    assert all(alvo != (None, None, "b") for alvo in alvos)
    assert len(regras) == 1


# --------------------------------------------------------------------------- fallback


def test_fallback_get_cru_quando_o_chrome_falha(tmp_path, monkeypatch):
    """Sem Chrome a tool não morre: cai no GET cru, e DIZ que caiu.

    A etiqueta importa — no GET cru não existe nó injetado por script, e o modelo
    precisa saber disso antes de concluir que o nó não chegou.
    """
    mudo = tmp_path / "fake-chrome-mudo"
    mudo.write_text(FAKE_CHROME_MUDO.format(python=sys.executable), encoding="utf-8")
    mudo.chmod(mudo.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(uiverify.CHROME_ENV, str(mudo))

    raiz = tmp_path / "site"
    raiz.mkdir()
    (raiz / "index.html").write_text('<html><body><p id="l">servido</p></body></html>', encoding="utf-8")

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(raiz), **kw)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((procs.LOOPBACK, 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ws = tmp_path / "ws"
        (ws / ".harness").mkdir(parents=True)
        procs.procs_path(ws).write_text(
            json.dumps([{"id": "s", "pid": os.getpid(), "pgid": os.getpid(), "port": httpd.server_address[1]}]),
            encoding="utf-8",
        )

        out = dom_tools.inspect_dom(ws, httpd.server_address[1], "#l")
    finally:
        httpd.shutdown()

    assert "existe: sim" in out
    assert "GET cru" in out
    assert "servido" in out


def test_make_dom_tools_expoe_as_duas():
    pytest.importorskip("langchain_core")

    nomes = [t.name for t in dom_tools.make_dom_tools("/tmp")]

    assert nomes == ["inspect_dom", "a11y_audit"]


def test_tools_devolvem_string_em_vez_de_exceção(monkeypatch):
    """Tool node não pode receber exceção — nem quando o miolo estoura."""
    pytest.importorskip("langchain_core")

    def explode(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(dom_tools, "fetch_dom", explode)
    inspecionar, auditar = dom_tools.make_dom_tools("/tmp")

    assert "inspect_dom falhou: RuntimeError: boom" in inspecionar.invoke({"selector": "div", "port": 1})
    assert "a11y_audit falhou: RuntimeError: boom" in auditar.invoke({"port": 1})
