"""`harness ui-verify`: o caso que motivou o comando é o `test_link_morto` aqui.

Um `dist/` com `<link rel="stylesheet" href="../styles/global.css">` e nenhum CSS
dentro do dist compila, sai 0 no build, passa em todo grep do HTML — e chega cru
no navegador. Este arquivo é a régua reproduzindo isso em miniatura.

O Chrome é substituído por um script fake: o check de screenshot que interessa
testar é "o PNG saiu e tem tamanho de página com conteúdo", e amarrar a suíte a
um navegador instalado quebraria o CI (que roda ubuntu, sem Chrome). O navegador
de verdade foi exercitado nos dois workspaces reais do site.
"""

import stat
import sys

import pytest

from harness import cli, uiverify
from harness.graph import run_graph

CSS = "body{color:#111;font-family:system-ui}\n" * 20

# Fake do Chrome: lê `--screenshot=<path>` e escreve um PNG íntegro (assinatura +
# IEND) do tamanho pedido em FAKE_SHOT_BYTES.
FAKE_CHROME = '''#!{python}
import os, sys
out = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--screenshot="))
n = int(os.environ.get("FAKE_SHOT_BYTES", "40000"))
with open(out, "wb") as fh:
    fh.write(b"\\x89PNG\\r\\n\\x1a\\n" + b"x" * n + b"IEND\\xaeB`\\x82")
'''


@pytest.fixture(autouse=True)
def fake_chrome(tmp_path, monkeypatch):
    exe = tmp_path / "fake-chrome"
    exe.write_text(FAKE_CHROME.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(uiverify.CHROME_ENV, str(exe))
    monkeypatch.setenv("FAKE_SHOT_BYTES", "40000")
    return exe


def make_dist(tmp_path, head, body="<h1>oi</h1>", css=True):
    """Um dist mínimo: `index.html` com o `<head>` que o teste quiser."""
    dist = tmp_path / "dist"
    (dist / "_astro").mkdir(parents=True)
    if css:
        (dist / "_astro" / "site.css").write_text(CSS, encoding="utf-8")
    (dist / "index.html").write_text(
        f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>",
        encoding="utf-8",
    )
    return dist


LINK_OK = '<link rel="stylesheet" href="/_astro/site.css">'
LINK_MORTO = '<link rel="stylesheet" href="../styles/global.css">'


def test_dist_com_css_passa(tmp_path):
    res = uiverify.verify(make_dist(tmp_path, LINK_OK), expect=("css",),
                          shot_out=tmp_path / "shot.png")
    assert res.failures == ()
    assert (res.checked, res.ok_assets) == (1, 1)
    assert res.shot.is_file()


def test_link_morto_reprova_com_os_dois_motivos(tmp_path):
    """O dist real quebrado: o `<link>` existe, o arquivo não. Duas falhas —
    o asset 404 e a ausência de qualquer folha carregável."""
    res = uiverify.verify(make_dist(tmp_path, LINK_MORTO, css=False), expect=("css",),
                          shot_out=tmp_path / "shot.png")
    assert res.failures == (
        "asset 404: ../styles/global.css",
        "css: nenhum stylesheet carregável (nem <style> inline)",
    )


def test_asset_404_reprova_sem_expect_asset(tmp_path):
    """Imagem morta derruba o verify mesmo sem `--expect-asset`: o check de
    referência local não depende de flag."""
    res = uiverify.verify(make_dist(tmp_path, LINK_OK, body='<img src="/foto.jpg">'),
                          shot_out=tmp_path / "shot.png")
    assert res.failures == ("asset 404: /foto.jpg",)


def test_referencia_externa_nao_e_conferida(tmp_path):
    """A régua responde pelo que o build gerou, não pela internet de quem rodou."""
    head = (
        LINK_OK
        + '<link rel="preconnect" href="https://fonts.googleapis.com">'
        + '<script src="//cdn.example.com/x.js"></script>'
    )
    body = '<a href="mailto:x@y.z">m</a><a href="#topo">t</a>'
    res = uiverify.verify(make_dist(tmp_path, head, body=body), expect=("css",),
                          shot_out=tmp_path / "shot.png")
    assert res.failures == ()
    assert res.checked == 1


def test_style_inline_conta_como_css(tmp_path):
    """O Astro inlina folha pequena; exigir `<link>` reprovaria build legítimo."""
    res = uiverify.verify(make_dist(tmp_path, f"<style>{CSS}</style>", css=False),
                          expect=("css",), shot_out=tmp_path / "shot.png")
    assert res.failures == ()


def test_screenshot_pequeno_reprova(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SHOT_BYTES", "500")
    res = uiverify.verify(make_dist(tmp_path, LINK_OK), shot_out=tmp_path / "shot.png")
    assert len(res.failures) == 1
    assert res.failures[0].startswith("screenshot 0.5kb < 20.0kb")


def test_sem_chrome_e_falha_nao_aviso(tmp_path, monkeypatch):
    """Navegador ausente significa "não foi verificado", nunca "passou"."""
    monkeypatch.setenv(uiverify.CHROME_ENV, "chrome-que-nao-existe")
    res = uiverify.verify(make_dist(tmp_path, LINK_OK), shot_out=tmp_path / "shot.png")
    assert res.failures == (uiverify.MISSING_CHROME,)


@pytest.mark.parametrize(
    "stdout, esperado",
    [
        ('{"ok": true, "motivo": "tem estilo"}', (True, "tem estilo")),
        ('```json\n{"ok": false, "motivo": "cru"}\n```', (False, "cru")),
        ('Claro! {"ok": true, "motivo": "x"}', None),
        ('{"ok": "sim", "motivo": "x"}', None),
        ("", None),
    ],
)
def test_parse_ask_estrito(stdout, esperado):
    """Cerca de código é formatação e sai; prosa em volta do JSON é falha —
    régua que adivinha o veredito não é régua."""
    assert uiverify.parse_ask(stdout) == esperado


def test_cli_exit_code(tmp_path, monkeypatch, capsys):
    """O que a unidade chama de verdade é a CLI, e o que ela lê é o exit code."""
    dist = make_dist(tmp_path, LINK_MORTO, css=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["ui-verify", str(dist), "--expect-asset", "css"]) == 1
    assert cli.main(["ui-verify", str(make_dist(tmp_path / "bom", LINK_OK)),
                     "--expect-asset", "css"]) == 0
    assert (tmp_path / uiverify.SHOT_NAME).is_file()
    assert "falhas=0" in capsys.readouterr().out


def test_policy_le_toggle_do_graph_toml(tmp_path):
    """O hook do grafo é opt-in: sem `nodes.ui_verify` ninguém abre navegador."""
    assert run_graph.load_policy(tmp_path / "nao-existe.toml").ui_verify is False
    toml = tmp_path / "graph.toml"
    toml.write_text('ui_verify_dist = "build"\n[nodes]\nui_verify = true\n',
                    encoding="utf-8")
    policy = run_graph.load_policy(toml)
    assert (policy.ui_verify, policy.ui_verify_dist) == (True, "build")


def test_hook_do_grafo_devolve_falha_da_tela(tmp_path):
    """O `dist/` quebrado dentro do workspace vira falha no veredito do run."""
    make_dist(tmp_path, LINK_MORTO, css=False)
    policy = run_graph.GraphPolicy(ui_verify=True)
    assert run_graph._ui_verify(tmp_path, policy) == [
        "asset 404: ../styles/global.css",
        "css: nenhum stylesheet carregável (nem <style> inline)",
    ]


def test_hook_do_grafo_falha_aberto(tmp_path, monkeypatch):
    """Sem dist e sem navegador não há tela para olhar: o run não é reprovado
    por isso — quem quer rigor põe `harness ui-verify` no `verify_cmd`."""
    policy = run_graph.GraphPolicy(ui_verify=True)
    assert run_graph._ui_verify(tmp_path, policy) == []
    make_dist(tmp_path, LINK_OK)
    monkeypatch.setenv(uiverify.CHROME_ENV, "chrome-que-nao-existe")
    assert run_graph._ui_verify(tmp_path, policy) == []


def test_dist_inexistente_reprova(tmp_path):
    res = uiverify.verify(tmp_path / "nao-existe")
    assert len(res.failures) == 1 and "dist não é diretório" in res.failures[0]


def test_url_path_aponta_para_outra_pagina(tmp_path):
    dist = make_dist(tmp_path, LINK_OK)
    (dist / "sobre").mkdir()
    (dist / "sobre" / "index.html").write_text(
        f"<!DOCTYPE html><html><head>{LINK_MORTO}</head><body>x</body></html>",
        encoding="utf-8",
    )
    res = uiverify.verify(dist, url_path="/sobre/", expect=("css",),
                          shot_out=tmp_path / "shot.png")
    assert res.failures[0] == "asset 404: ../styles/global.css"


def test_pagina_inexistente_reprova(tmp_path):
    res = uiverify.verify(make_dist(tmp_path, LINK_OK), url_path="/faltando/",
                          shot_out=tmp_path / "shot.png")
    assert res.failures == ("página /faltando/ respondeu 404",)
    assert res.shot is None
