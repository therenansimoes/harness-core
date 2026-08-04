"""Testes das file tools inteligentes: guarda de leitura, shrink-guard e as
tools de edição por número de linha (outline/edit_range/insert_lines/append)."""

import pytest

from harness.backends import file_tools as ft

pytest.importorskip("deepagents", reason="extra deepagents não instalado")

from harness.backends.safe_shell import SafeShellBackend  # noqa: E402
from harness.backends.smart_fs import (  # noqa: E402
    BIG_FILE_LINES,
    GUARD_HEAD_LINES,
    SmartFilesystemMiddleware,
)

PY_MODULE = """import os


class Alvo:
    def metodo(self):
        return 1


@deco
def solto():
    pass
"""


def _runtime():
    """ToolRuntime mínimo: fora do grafo ninguém injeta, e as tools de arquivo
    só usam `tool_call_id`."""
    from langchain.tools import ToolRuntime

    return ToolRuntime(
        state={"messages": []},
        context=None,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-1",
        store=None,
    )


def _fs_tools(root):
    mw = SmartFilesystemMiddleware(
        backend=SafeShellBackend(root_dir=str(root), virtual_mode=True),
        tools=["ls", "read_file", "write_file", "edit_file"],
    )
    return mw, {t.name: t for t in mw.tools}


def _big(tmp_path, lines=5000):
    path = tmp_path / "big.py"
    path.write_text("".join(f"x{i} = {i}\n" for i in range(1, lines + 1)), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# middleware: identidade e guarda de leitura
# --------------------------------------------------------------------------- #


def test_middleware_se_passa_pelo_original(tmp_path):
    # o merge do stack é por `.name`; nome de classe aqui duplicaria as tools
    mw, tools = _fs_tools(tmp_path)
    assert mw.name == "FilesystemMiddleware"
    assert [t.name for t in mw.tools] == ["ls", "read_file", "write_file", "edit_file"]
    assert set(tools["read_file"].args_schema.model_fields) == {"file_path", "offset", "limit"}


def test_read_sem_paginacao_em_arquivo_grande_corta_e_avisa(tmp_path):
    _big(tmp_path)
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["read_file"].func(file_path="/big.py", runtime=_runtime())

    assert "x60 = 60" in msg.content
    assert "x61 = 61" not in msg.content
    assert "TOTAL: 5000 linhas" in msg.content
    assert "KB" in msg.content
    assert "file_outline(path)" in msg.content


def test_read_com_offset_explicito_nao_sofre_guarda(tmp_path):
    _big(tmp_path)
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["read_file"].func(file_path="/big.py", runtime=_runtime(), offset=100, limit=5)

    assert "x101 = 101" in msg.content
    assert "x105 = 105" in msg.content
    assert "guarda de contexto" not in msg.content


def test_read_em_arquivo_pequeno_nao_muda(tmp_path):
    (tmp_path / "small.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["read_file"].func(file_path="/small.py", runtime=_runtime())

    assert "a = 1" in msg.content
    assert "b = 2" in msg.content
    assert "guarda de contexto" not in msg.content


def test_guarda_so_acima_do_limite(tmp_path):
    _big(tmp_path, lines=BIG_FILE_LINES)  # exatamente no limite: passa reto
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["read_file"].func(file_path="/big.py", runtime=_runtime())

    assert "guarda de contexto" not in msg.content
    assert f"x{GUARD_HEAD_LINES + 1} = {GUARD_HEAD_LINES + 1}" in msg.content


# --------------------------------------------------------------------------- #
# middleware: shrink-guard da escrita
# --------------------------------------------------------------------------- #


def test_shrink_guard_recusa_e_explica(tmp_path):
    big = _big(tmp_path)
    before = big.read_bytes()
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["write_file"].func(file_path="/big.py", content="x = 1\n", runtime=_runtime())

    assert msg.status == "error"
    assert "apagaria ~100% do arquivo" in msg.content
    assert "edit_range" in msg.content
    assert big.read_bytes() == before


def test_shrink_guard_deixa_passar_encolhimento_pequeno(tmp_path):
    alvo = tmp_path / "conf.txt"
    alvo.write_text("a" * 1000, encoding="utf-8")
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["write_file"].func(file_path="/conf.txt", content="b" * 800, runtime=_runtime())

    assert msg.status == "success"
    assert alvo.read_text(encoding="utf-8") == "b" * 800


def test_write_de_arquivo_novo_continua_ok(tmp_path):
    _mw, tools = _fs_tools(tmp_path)

    msg = tools["write_file"].func(file_path="/novo.py", content="y = 2\n", runtime=_runtime())

    assert msg.status == "success"
    assert (tmp_path / "novo.py").read_text(encoding="utf-8") == "y = 2\n"


# --------------------------------------------------------------------------- #
# file_outline
# --------------------------------------------------------------------------- #


def test_outline_py_traz_definicoes_e_decoradores(tmp_path):
    (tmp_path / "m.py").write_text(PY_MODULE, encoding="utf-8")

    out = ft.outline(tmp_path, "/m.py")

    assert "class Alvo:" in out
    assert "def metodo(self):" in out
    assert "@deco" in out
    assert "import os" not in out  # corpo não entra no mapa
    assert out.rstrip().endswith("TOTAL: 11 linhas, 0.1 KB")


def test_outline_md_traz_headings(tmp_path):
    (tmp_path / "r.md").write_text("# Titulo\ntexto solto\n## Secao\n", encoding="utf-8")

    out = ft.outline(tmp_path, "r.md")

    assert "# Titulo" in out
    assert "## Secao" in out
    assert "texto solto" not in out


def test_outline_json_nivel_1_e_2(tmp_path):
    (tmp_path / "p.json").write_text(
        '{"a": {"b": 1, "c": {"d": 2}}, "e": [1, 2, 3]}', encoding="utf-8"
    )

    out = ft.outline(tmp_path, "p.json")

    assert "a: objeto (2 chaves)" in out
    assert "a.b: número" in out
    assert "a.c: objeto (1 chaves)" in out
    assert "a.c.d" not in out  # nível 3 não entra
    assert "e: lista (3 itens)" in out


def test_outline_respeita_o_cap(tmp_path):
    corpo = "".join(f"def f{i}():\n    pass\n" for i in range(1, 301))
    (tmp_path / "muitas.py").write_text(corpo, encoding="utf-8")

    out = ft.outline(tmp_path, "muitas.py")

    assert out.count("def f") == ft.MAX_OUTLINE_ENTRIES
    assert f"cortado em {ft.MAX_OUTLINE_ENTRIES} entradas de 300" in out


# --------------------------------------------------------------------------- #
# edit_range
# --------------------------------------------------------------------------- #


def test_edit_range_substitui_e_renumera(tmp_path):
    alvo = tmp_path / "m.py"
    alvo.write_text(PY_MODULE, encoding="utf-8")

    out = ft.replace_range(
        tmp_path, "m.py", 5, 6, "    def metodo(self):\n        return 42", "    def metodo(self):"
    )

    assert "linhas 5-6 substituídas (2→2)" in out
    assert "validação: ok" in out
    assert "     5\t    def metodo(self):" in out
    assert "return 42" in alvo.read_text(encoding="utf-8")
    assert "return 1" not in alvo.read_text(encoding="utf-8")


def test_edit_range_expect_errado_nao_escreve_e_mostra_contexto(tmp_path):
    alvo = tmp_path / "m.py"
    alvo.write_text(PY_MODULE, encoding="utf-8")
    before = alvo.read_bytes()

    out = ft.replace_range(tmp_path, "m.py", 5, 5, "lixo", "def outra_coisa():")

    assert "expect_first_line não casa com a linha 5" in out
    assert "Contexto real (linhas 3-7)" in out
    assert "     5\t    def metodo(self):" in out
    assert alvo.read_bytes() == before


def test_edit_range_end_menor_que_start_aponta_insert_lines(tmp_path):
    (tmp_path / "m.py").write_text(PY_MODULE, encoding="utf-8")

    out = ft.replace_range(tmp_path, "m.py", 6, 4, "x = 1")

    assert "end_line=4 é menor que start_line=6" in out
    assert "insert_lines(path='m.py', after_line=5" in out


def test_edit_range_python_invalido_reverte_byte_a_byte(tmp_path):
    alvo = tmp_path / "m.py"
    alvo.write_text(PY_MODULE, encoding="utf-8")
    before = alvo.read_bytes()

    out = ft.replace_range(tmp_path, "m.py", 4, 4, "class Alvo(:")

    assert out.startswith("REVERTIDO:")
    assert "SyntaxError" in out
    assert alvo.read_bytes() == before
    # o backup do estado anterior fica no ring, mesmo com revert
    backups = list((tmp_path / ".harness" / "edits").iterdir())
    assert [b.read_bytes() for b in backups] == [before]


def test_edit_range_json_invalido_reverte(tmp_path):
    alvo = tmp_path / "p.json"
    alvo.write_text('{\n  "a": 1\n}\n', encoding="utf-8")
    before = alvo.read_bytes()

    out = ft.replace_range(tmp_path, "p.json", 2, 2, '  "a": 1,')

    assert out.startswith("REVERTIDO:")
    assert alvo.read_bytes() == before


def test_edit_range_conteudo_vazio_remove(tmp_path):
    alvo = tmp_path / "m.py"
    alvo.write_text(PY_MODULE, encoding="utf-8")

    out = ft.replace_range(tmp_path, "m.py", 1, 1, "")

    assert "linhas 1-1 removidas (1→0)" in out
    assert "import os" not in alvo.read_text(encoding="utf-8")


def test_edit_range_start_fora_do_arquivo(tmp_path):
    (tmp_path / "m.py").write_text(PY_MODULE, encoding="utf-8")

    out = ft.replace_range(tmp_path, "m.py", 999, 999, "x = 1")

    assert "passa do fim do arquivo (11 linhas)" in out
    assert "append_file" in out


# --------------------------------------------------------------------------- #
# insert_lines / append_file
# --------------------------------------------------------------------------- #


def test_insert_no_topo(tmp_path):
    alvo = tmp_path / "m.py"
    alvo.write_text(PY_MODULE, encoding="utf-8")

    out = ft.insert_after(tmp_path, "m.py", 0, "# cabecalho")

    assert "inseridas no topo" in out
    assert "     1\t# cabecalho" in out
    assert alvo.read_text(encoding="utf-8").startswith("# cabecalho\nimport os\n")


def test_insert_no_meio(tmp_path):
    alvo = tmp_path / "m.py"
    alvo.write_text(PY_MODULE, encoding="utf-8")

    out = ft.insert_after(tmp_path, "m.py", 1, "import sys")

    assert "depois da linha 1" in out
    linhas = alvo.read_text(encoding="utf-8").splitlines()
    assert linhas[:3] == ["import os", "import sys", ""]


def test_insert_depois_do_fim_recusa(tmp_path):
    (tmp_path / "m.py").write_text(PY_MODULE, encoding="utf-8")

    out = ft.insert_after(tmp_path, "m.py", 99, "x = 1")

    assert "passa do fim do arquivo (11 linhas)" in out


def test_append_em_arquivo_sem_newline_final(tmp_path):
    alvo = tmp_path / "notas.md"
    alvo.write_text("# notas", encoding="utf-8")

    out = ft.append(tmp_path, "notas.md", "- passo 2")

    assert "acrescentadas 1 linha(s) no fim" in out
    assert alvo.read_text(encoding="utf-8") == "# notas\n- passo 2\n"


def test_append_cria_arquivo(tmp_path):
    out = ft.append(tmp_path, "log.txt", "primeira linha")

    assert "criado com 1 linha(s)" in out
    assert (tmp_path / "log.txt").read_text(encoding="utf-8") == "primeira linha\n"


def test_backup_ring_limita_em_50(tmp_path):
    alvo = tmp_path / "conf.txt"
    alvo.write_text("v0\n", encoding="utf-8")
    for i in range(1, ft.BACKUP_RING + 11):
        ft.replace_range(tmp_path, "conf.txt", 1, 1, f"v{i}")

    backups = list((tmp_path / ".harness" / "edits").iterdir())
    assert len(backups) == ft.BACKUP_RING
    # o mais novo é o penúltimo estado, não o primeiro
    assert max(backups).read_bytes() == f"v{ft.BACKUP_RING + 9}\n".encode()


# --------------------------------------------------------------------------- #
# jail
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["../../etc/passwd", "/../../etc/passwd", "a/../../../etc/passwd"])
def test_jail_bloqueia_saida_do_workspace(tmp_path, path):
    assert ft.outline(tmp_path, path).startswith("Erro: caminho fora do workspace")
    assert ft.replace_range(tmp_path, path, 1, 1, "x").startswith("Erro: caminho fora do workspace")
    assert ft.insert_after(tmp_path, path, 0, "x").startswith("Erro: caminho fora do workspace")
    assert ft.append(tmp_path, path, "x").startswith("Erro: caminho fora do workspace")


def test_jail_nao_cria_arquivo_fora(tmp_path):
    fora = tmp_path.parent / "vazado.txt"

    ft.append(tmp_path, f"../{fora.name}", "conteudo")

    assert not fora.exists()


# --------------------------------------------------------------------------- #
# tools LangChain
# --------------------------------------------------------------------------- #


def test_make_file_tools_expoe_nomes_novos(tmp_path):
    pytest.importorskip("langchain_core")
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")

    tools = {t.name: t for t in ft.make_file_tools(tmp_path)}

    assert set(tools) == {"file_outline", "edit_range", "insert_lines", "append_file"}
    assert "def f():" in tools["file_outline"].invoke({"path": "/a.py"})
    assert "substituídas" in tools["edit_range"].invoke(
        {"path": "/a.py", "start_line": 2, "end_line": 2, "new_content": "    return 1"}
    )
    assert "no topo" in tools["insert_lines"].invoke(
        {"path": "/a.py", "after_line": 0, "content": "# topo"}
    )
    assert "no fim" in tools["append_file"].invoke({"path": "/a.py", "content": "# fim"})
