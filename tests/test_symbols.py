"""Testes do índice de símbolos: extração por linguagem, cache por mtime/size,
arquivo apagado, string que não é referência e teto de arquivos."""

import pytest

from harness import symbols as sy

PY_MODULE = '''"""Docstring com a palavra Alvo dentro, que não é referência."""


class Alvo:
    """Classe de teste."""

    def metodo(self, x: int = 3) -> str:
        return str(x)


@deco
async def solto(a, b):
    return Alvo()
'''

JS_MODULE = """// comentário com handleSubmit dentro
import { x } from "./x.js";

export function handleSubmit(evt) {
  const rotulo = "handleSubmit foi chamado";
  return rotulo;
}

export class Painel {}

const total = 3;
export const soma = (a, b) => a + b;
"""

HTML_PAGE = """<!doctype html>
<!-- comentário com id="fantasma" -->
<html lang="pt-BR">
  <body>
    <nav id="menu"><a href="#preco">preço</a></nav>
    <main id="conteudo">
      <section id="preco"><h2>Preço</h2></section>
      <div id="rodape"></div>
    </main>
  </body>
</html>
"""


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "mod.py").write_text(PY_MODULE, encoding="utf-8")
    (tmp_path / "app" / "ui.js").write_text(JS_MODULE, encoding="utf-8")
    (tmp_path / "index.html").write_text(HTML_PAGE, encoding="utf-8")
    # Ruído que o índice tem de ignorar.
    lixo = tmp_path / "node_modules" / "pkg"
    lixo.mkdir(parents=True)
    (lixo / "dep.js").write_text("export function handleSubmit() {}\n", encoding="utf-8")
    return tmp_path


def _por_nome(achados):
    return {a["name"]: a for a in achados}


def test_py_classe_e_metodo_com_assinatura_exata(ws):
    indice = sy.index_workspace(ws)

    assert indice["Alvo"] == [("app/mod.py", 4, "class", "class Alvo:")]
    assert indice["metodo"] == [("app/mod.py", 7, "def", "def metodo(self, x: int = 3) -> str:")]
    # Decorado entra, e a linha é a do `async def`, não a do decorator.
    assert indice["solto"] == [("app/mod.py", 12, "async def", "async def solto(a, b):")]
    assert sy.signature_of(ws, "metodo") == "def metodo(self, x: int = 3) -> str:"


def test_find_symbol_exato_e_prefixo_com_path_relativo(ws):
    exato = sy.find_symbol(ws, "Alvo")
    assert [(a["path"], a["line"], a["kind"]) for a in exato] == [("app/mod.py", 4, "class")]

    prefixo = _por_nome(sy.find_symbol(ws, "hand"))
    assert prefixo["handleSubmit"]["path"] == "app/ui.js"
    assert prefixo["handleSubmit"]["kind"] == "function"


def test_js_por_linguagem_e_node_modules_fora(ws):
    indice = sy.index_workspace(ws)

    assert [(p, k) for p, _, k, _ in indice["handleSubmit"]] == [("app/ui.js", "function")]
    assert indice["Painel"][0][2] == "class"
    assert indice["total"][0][2] == "const"
    assert indice["soma"][0][2] == "function"  # arrow com nome é função
    assert "dep.js" not in " ".join(p for oc in indice.values() for p, *_ in oc)


def test_html_ids_e_landmarks(ws):
    indice = sy.index_workspace(ws)

    assert indice["menu"][0][:3] == ("index.html", 5, "nav")
    assert indice["conteudo"][0][2] == "main"
    assert indice["preco"][0][2] == "section"
    assert indice["rodape"][0][2] == "id"
    assert "fantasma" not in indice  # id dentro de comentário HTML não existe


def test_segunda_chamada_nao_rele_arquivo_intocado(ws, monkeypatch):
    sy.index_workspace(ws)

    lidos = []
    original = sy._read
    monkeypatch.setattr(sy, "_read", lambda p: (lidos.append(str(p)), original(p))[1])

    assert sy.index_workspace(ws) and lidos == []

    (ws / "app" / "mod.py").write_text(PY_MODULE + "\n\ndef novo():\n    pass\n", encoding="utf-8")
    indice = sy.index_workspace(ws)

    assert [p for p in lidos if p.endswith("mod.py")]
    assert not [p for p in lidos if p.endswith("ui.js")]  # intocado segue no cache
    assert "novo" in indice


def test_arquivo_deletado_sai_do_indice(ws):
    assert "handleSubmit" in sy.index_workspace(ws)

    (ws / "app" / "ui.js").unlink()
    indice = sy.index_workspace(ws)

    assert "handleSubmit" not in indice
    assert "app/ui.js" not in sy._load(ws)
    assert "Alvo" in indice  # o resto do índice fica de pé


def test_nome_em_string_ou_comentario_nao_e_referencia(ws):
    refs = sy.find_references(ws, "handleSubmit")

    assert [(r["path"], r["line"]) for r in refs] == [("app/ui.js", 4)]

    # Em Python, docstring é string: não conta como uso de `Alvo`.
    linhas_py = [r["line"] for r in sy.find_references(ws, "Alvo")]
    assert linhas_py == [4, 13]


def test_referencias_no_topo_de_20(ws):
    corpo = "".join(f"const v{i} = soma(1, {i});\n" for i in range(30))
    (ws / "app" / "muitos.js").write_text(corpo, encoding="utf-8")

    assert len(sy.find_references(ws, "soma")) == sy.MAX_HITS


def test_teto_de_arquivos_respeitado(ws, monkeypatch):
    monkeypatch.setattr(sy, "MAX_FILES", 3)
    alvo = ws / "muitos"
    alvo.mkdir()
    for i in range(10):
        (alvo / f"f{i}.py").write_text(f"def f{i}():\n    pass\n", encoding="utf-8")

    sy.index_workspace(ws)

    assert len(sy._load(ws)) == 3


def test_make_symbol_tools_expoe_as_tres(ws):
    pytest.importorskip("langchain_core", reason="extra deepagents não instalado")

    tools = {t.name: t for t in sy.make_symbol_tools(ws)}
    assert set(tools) == {"find_symbol", "find_references", "signature_of"}

    saida = tools["find_symbol"].invoke({"name": "Alvo"})
    assert "app/mod.py:4" in saida and "class Alvo:" in saida
    assert "não está no índice" in tools["signature_of"].invoke({"name": "inexistente"})
