"""Cerca e tools de web, 100% offline: nenhum teste aqui abre socket.

O DNS é substituído por `ssrf._resolve` monkeypatchado e o HTTP por um opener
falso. O que se verifica é a decisão da cerca, não a internet.
"""

import urllib.error
from pathlib import Path

import pytest

from harness.backends import ssrf, web_tools
from harness.backends.ssrf import UrlBlocked, WebConfig, WebConfigError, load_web_config

FIXTURES = Path(__file__).parent / "fixtures"

LIBERADO = WebConfig(enabled=True)
PUBLICO = "93.184.216.34"

CONFIG_OK = """\
[web]
enabled = true
browse_enabled = true
allowlist = ["exemplo.com"]
"""


def _config(tmp_path, texto):
    p = tmp_path / "web.toml"
    p.write_text(texto, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- cerca

BLOQUEADAS = [
    ("http://127.0.0.1", "loopback"),
    ("http://169.254.169.254/latest/meta-data/", "link-local"),
    ("http://[::1]", "loopback"),
    ("http://10.0.0.1", "private"),
    ("file:///etc/passwd", "scheme"),
    ("http://u:p@x.com", "userinfo"),
    ("http://x.com:22", "porta"),
    ("http://[::ffff:127.0.0.1]/", "loopback"),
    ("http://0.0.0.0", "unspecified"),
    ("http://224.0.0.1", "multicast"),
    ("https://x.com/" + "a" * 3000, "bytes"),
]


@pytest.mark.parametrize("url,motivo", BLOQUEADAS, ids=[u for u, _ in BLOQUEADAS])
def test_tabela_de_bloqueio(url, motivo, monkeypatch):
    # Nenhuma dessas URLs deve chegar ao DNS: se chegar, o teste falha aqui.
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: pytest.fail(f"resolveu {h}"))
    with pytest.raises(UrlBlocked) as exc:
        ssrf.assert_url_allowed(url, LIBERADO)
    assert motivo in str(exc.value)


def test_host_publico_passa(monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO])
    assert ssrf.assert_url_allowed("https://exemplo.com/doc", LIBERADO) == [PUBLICO]


def test_dns_rebinding_um_endereco_privado_reprova_tudo(monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO, "127.0.0.1"])
    with pytest.raises(UrlBlocked, match="loopback"):
        ssrf.assert_url_allowed("https://exemplo.com", LIBERADO)


def test_denylist_vence_allowlist(monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO])
    cfg = WebConfig(enabled=True, allowlist=("exemplo.com",), denylist=("interno.exemplo.com",))
    assert ssrf.assert_url_allowed("https://exemplo.com", cfg) == [PUBLICO]
    with pytest.raises(UrlBlocked, match="denylist"):
        ssrf.assert_url_allowed("https://interno.exemplo.com", cfg)


def test_allowlist_nao_vazia_exclui_o_resto(monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO])
    cfg = WebConfig(enabled=True, allowlist=("exemplo.com",))
    assert ssrf.assert_url_allowed("https://docs.exemplo.com", cfg)  # subdomínio casa
    with pytest.raises(UrlBlocked, match="fora da allowlist"):
        ssrf.assert_url_allowed("https://outro.com", cfg)


def test_extra_ports_do_config(monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO])
    with pytest.raises(UrlBlocked, match="porta 8080"):
        ssrf.assert_url_allowed("http://exemplo.com:8080/x", LIBERADO)
    liberada = WebConfig(enabled=True, extra_ports=frozenset({8080}))
    assert ssrf.assert_url_allowed("http://exemplo.com:8080/x", liberada)


def test_web_desabilitada_bloqueia_tudo():
    with pytest.raises(UrlBlocked, match="web desabilitada"):
        ssrf.assert_url_allowed("https://exemplo.com", WebConfig(enabled=False))


# --------------------------------------------------------------------------- redirect


class _FakeHeaders(dict):
    def get(self, k, default=None):  # urllib chama .get case-insensitive na prática
        return dict.get(self, k, dict.get(self, k.lower(), default))


def _hop(monkeypatch, destino):
    """Simula o hop de redirect: o handler decide se a próxima URL pode."""
    import urllib.request

    mapa = {"publico.exemplo": PUBLICO, "evil.interno": "127.0.0.1"}
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [mapa.get(h, PUBLICO)])
    handler = ssrf.redirect_handler(LIBERADO)
    req = urllib.request.Request("http://publico.exemplo/")
    return handler.redirect_request(req, None, 302, "Found", _FakeHeaders(), destino)


def test_redirect_para_loopback_bloqueado_no_segundo_hop(monkeypatch):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _hop(monkeypatch, "http://evil.interno/latest/meta-data/")
    assert "redirect bloqueado" in str(exc.value)
    assert "loopback" in str(exc.value)


def test_redirect_para_publico_segue(monkeypatch):
    assert _hop(monkeypatch, "http://publico.exemplo/outra") is not None


def test_maximo_de_tres_redirects():
    assert ssrf.redirect_handler(LIBERADO).max_redirections == 3


# --------------------------------------------------------------------------- HTTP falso


class _FakeResp:
    def __init__(self, corpo: bytes, ctype: str):
        self._corpo, self._i = corpo, 0
        self.headers = _FakeHeaders({"Content-Type": ctype})

    def read(self, n=None):
        pedaco = self._corpo[self._i : self._i + n] if n else self._corpo[self._i :]
        self._i += len(pedaco)
        return pedaco

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_http(monkeypatch, corpo: bytes, ctype: str = "text/html"):
    vistas = []

    class _Opener:
        def open(self, req, timeout=None):
            vistas.append(req.full_url)
            return _FakeResp(corpo, ctype)

    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO])
    monkeypatch.setattr(web_tools, "opener", lambda cfg: _Opener())
    return vistas


def test_corte_em_500kb(tmp_path, monkeypatch):
    corpo = b"<html><body><p>" + b"x" * (900 * 1024) + b"</p></body></html>"
    _fake_http(monkeypatch, corpo)
    bruto, ctype, truncou = web_tools._abre("https://exemplo.com/grande", LIBERADO)
    assert len(bruto) == web_tools.MAX_BYTES
    assert truncou is True and ctype == "text/html"

    saida = web_tools.web_fetch("https://exemplo.com/grande", workspace=tmp_path, cfg=LIBERADO)
    assert web_tools.UNTRUSTED_HEADER in saida
    assert "bytes: 512000" in saida
    # a marca de truncamento fica no fim do texto — visível no cache, não na janela
    cache = next((tmp_path / ".harness/webcache").glob("*.txt")).read_text(encoding="utf-8")
    assert cache.endswith("[truncado em 512000 bytes baixados]")


def test_web_fetch_grava_cache_janela_e_audit(tmp_path, monkeypatch):
    html = (FIXTURES / "pagina_web.html").read_bytes()
    _fake_http(monkeypatch, html)
    saida = web_tools.web_fetch("https://exemplo.com/guia", workspace=tmp_path, cfg=LIBERADO)

    assert saida.startswith(web_tools.UNTRUSTED_HEADER)
    assert saida.rstrip().endswith(web_tools.UNTRUSTED_FOOTER)
    assert "Guia rápido" in saida and "rastreador" not in saida

    cache = list((tmp_path / ".harness/webcache").glob("*.txt"))
    assert len(cache) == 1 and "Guia rápido" in cache[0].read_text(encoding="utf-8")
    assert f"/.harness/webcache/{cache[0].name}" in saida

    log = (tmp_path / ".harness/web.log").read_text(encoding="utf-8").strip().split("\t")
    assert log[1] == "https://exemplo.com/guia" and int(log[2]) == len(html)


def test_web_fetch_pagina_grande_pagina_por_offset(tmp_path, monkeypatch):
    _fake_http(monkeypatch, b"<html><body><p>" + b"ab" * 6000 + b"</p></body></html>")
    inicio = web_tools.web_fetch("https://exemplo.com/x", workspace=tmp_path, cfg=LIBERADO)
    assert "janela: 0..6000" in inicio and "read_file" in inicio
    resto = web_tools.web_fetch("https://exemplo.com/x", offset=6000, workspace=tmp_path, cfg=LIBERADO)
    assert "janela: 6000.." in resto and "fim do conteúdo" in resto


def test_conteudo_nao_textual_vira_resumo(tmp_path, monkeypatch):
    _fake_http(monkeypatch, b"%PDF-1.7 binario", ctype="application/pdf")
    saida = web_tools.web_fetch("https://exemplo.com/a.pdf", workspace=tmp_path, cfg=LIBERADO)
    assert saida == "tipo application/pdf, 16 bytes"


def test_web_fetch_url_bloqueada_nao_levanta(tmp_path):
    saida = web_tools.web_fetch("http://169.254.169.254/latest/meta-data/", workspace=tmp_path, cfg=LIBERADO)
    assert saida.startswith("web_fetch bloqueado pela cerca:") and "link-local" in saida


# --------------------------------------------------------------------------- extrator


def test_extrator_em_fixture():
    texto = web_tools.extract_text((FIXTURES / "pagina_web.html").read_text(encoding="utf-8"))
    assert "Guia rápido" in texto
    assert "Primeiro parágrafo com & entidade." in texto
    assert "Segundo parágrafo com espaço demais." in texto  # espaço colapsado
    assert "item um" in texto and "item dois" in texto
    assert "bloco final\ncom quebra" in texto
    for lixo in ("<p>", "rastreador", "color: #333", "ignore suas instrucoes", "ative o javascript", "M0 0h10"):
        assert lixo not in texto
    assert "Titulo no head" not in texto  # head sai inteiro
    assert "\n\n\n" not in texto  # blank lines colapsadas


# --------------------------------------------------------------------------- busca


def test_parser_ddg_em_fixture():
    resultados = web_tools.parse_ddg((FIXTURES / "ddg_html.html").read_text(encoding="utf-8"), k=8)
    assert [r["url"] for r in resultados] == [
        "https://docs.python.org/3/library/tomllib.html",  # /l/?uddg= desembrulhado
        "https://pypi.org/project/tomli/",
    ]
    assert resultados[0]["title"] == "tomllib — Parse TOML files — Python 3.13"
    assert resultados[0]["snippet"].startswith("tomllib provides an interface")
    assert len(resultados[1]["snippet"]) == web_tools.SNIPPET_CHARS


def test_web_search_usa_ddg_html_e_embrulha(tmp_path, monkeypatch):
    vistas = _fake_http(monkeypatch, (FIXTURES / "ddg_html.html").read_bytes())
    monkeypatch.setattr(web_tools, "_rate_limit", lambda: None)
    saida = web_tools.web_search("tomllib", workspace=tmp_path, cfg=LIBERADO)
    assert vistas == ["https://html.duckduckgo.com/html/?q=tomllib"]  # não caiu pro lite
    assert web_tools.UNTRUSTED_HEADER in saida
    assert "1. tomllib — Parse TOML files" in saida


def test_web_search_cai_para_o_lite_e_falha_aberto(tmp_path, monkeypatch):
    vistas = _fake_http(monkeypatch, b"<html><body>sem resultados</body></html>")
    monkeypatch.setattr(web_tools, "_rate_limit", lambda: None)
    cfg = WebConfig(enabled=True, searx_url="https://searx.exemplo.com/search")
    saida = web_tools.web_search("nada", workspace=tmp_path, cfg=cfg)
    assert len(vistas) == 3 and "lite.duckduckgo.com" in vistas[1] and "searx" in vistas[2]
    assert saida.startswith("web_search: nenhum resultado")


def test_orcamento_de_busca(monkeypatch):
    monkeypatch.setattr(web_tools, "_busca_gastas", web_tools.SEARCH_BUDGET)
    assert "orçamento de busca" in web_tools.web_search("x", cfg=LIBERADO)


# --------------------------------------------------------------------------- browse


def test_browse_fora_da_allowlist_nao_chama_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: pytest.fail("resolveu DNS"))
    monkeypatch.setattr(
        web_tools.subprocess, "run", lambda *a, **k: pytest.fail("subprocess foi chamado")
    )
    cfg = WebConfig(enabled=True, browse_enabled=True, allowlist=("exemplo.com",))
    assert "allowlist" in web_tools.browse("https://outro.com/x", workspace=tmp_path, cfg=cfg)
    # allowlist vazia = nada, o oposto do web_fetch
    vazia = WebConfig(enabled=True, browse_enabled=True)
    assert "allowlist" in web_tools.browse("https://exemplo.com/x", workspace=tmp_path, cfg=vazia)


def test_browse_desabilitado_por_default(tmp_path, monkeypatch):
    monkeypatch.setattr(
        web_tools.subprocess, "run", lambda *a, **k: pytest.fail("subprocess foi chamado")
    )
    cfg = WebConfig(enabled=True, allowlist=("exemplo.com",))
    assert "browse_enabled" in web_tools.browse("https://exemplo.com/x", workspace=tmp_path, cfg=cfg)


def test_browse_na_allowlist_roda_chrome_com_os_flags(tmp_path, monkeypatch):
    from harness import uiverify

    monkeypatch.setattr(ssrf, "_resolve", lambda h, p: [PUBLICO])
    monkeypatch.setattr(uiverify, "chrome", lambda: "/bin/chrome-falso")
    argvs = []

    class _Proc:
        returncode = 0
        stdout = b"<html><body><h1>renderizado</h1></body></html>"
        stderr = b""

    def _run(argv, **kwargs):
        argvs.append((argv, kwargs))
        return _Proc()

    monkeypatch.setattr(web_tools.subprocess, "run", _run)
    cfg = WebConfig(enabled=True, browse_enabled=True, allowlist=("exemplo.com",))
    saida = web_tools.browse("https://exemplo.com/app", workspace=tmp_path, cfg=cfg)

    argv, kwargs = argvs[0]
    assert argv[0] == "/bin/chrome-falso" and argv[-1] == "https://exemplo.com/app"
    assert "--headless=new" in argv and "--dump-dom" in argv
    assert "--virtual-time-budget=5000" in argv
    assert any(a.startswith("--user-data-dir=") for a in argv)
    assert kwargs["shell"] is False and kwargs["timeout"] == web_tools.BROWSE_TIMEOUT_S
    assert "renderizado" in saida and web_tools.UNTRUSTED_HEADER in saida


# --------------------------------------------------------------------------- config


def test_config_valido(tmp_path):
    cfg = load_web_config(_config(tmp_path, CONFIG_OK))
    assert cfg.enabled and cfg.browse_enabled and cfg.allowlist == ("exemplo.com",)


def test_config_ausente_desabilita(tmp_path):
    with pytest.raises(WebConfigError, match="não existe"):
        load_web_config(tmp_path / "nao_existe.toml")


@pytest.mark.parametrize("texto", ["isto não é toml [[[", '[web]\nallowlist = "exemplo.com"\n'])
def test_config_invalido_tools_erram_claro(tmp_path, texto):
    p = _config(tmp_path, texto)
    with pytest.raises(WebConfigError):
        load_web_config(p)

    for saida in (
        web_tools.web_fetch("https://exemplo.com", workspace=tmp_path, config_path=p),
        web_tools.web_search("x", workspace=tmp_path, config_path=p),
        web_tools.browse("https://exemplo.com", workspace=tmp_path, config_path=p),
    ):
        assert saida.startswith("web indisponível:") and str(p) in saida

    assert web_tools.load_web_tools(tmp_path, p) == []  # loader é fail-open


def test_load_web_tools_respeita_enabled(tmp_path):
    assert web_tools.load_web_tools(tmp_path, _config(tmp_path, "[web]\nenabled = false\n")) == []


def test_load_web_tools_exporta_as_tres(tmp_path):
    nomes = [t.name for t in web_tools.load_web_tools(tmp_path, _config(tmp_path, CONFIG_OK))]
    assert nomes == ["web_fetch", "web_search", "browse"]

    sem_browse = web_tools.load_web_tools(tmp_path, _config(tmp_path, "[web]\nenabled = true\n"))
    assert [t.name for t in sem_browse] == ["web_fetch", "web_search"]


def test_config_do_repo_e_valido():
    cfg = load_web_config("config/web.toml")
    assert cfg.enabled and not cfg.browse_enabled


def test_web_tools_lazy_export():
    # WEB_TOOLS existe mas só monta as tools quando alguém pede (LangChain lazy).
    assert isinstance(web_tools.WEB_TOOLS, list)
