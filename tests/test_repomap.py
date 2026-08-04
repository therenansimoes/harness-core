"""Testes do repo_map: quem é referenciado sobe, orçamento corta, cache repete."""

from harness import repomap as rm

CORE = '''"""Núcleo."""


def alpha(x):
    return x + 1


def nao_usada():
    return 0
'''

IMPORTADOR = """from core import alpha


def usa_{n}():
    return alpha(alpha({n}))
"""


def _repo(tmp_path):
    (tmp_path / "core.py").write_text(CORE, encoding="utf-8")
    for n in (1, 2, 3):
        (tmp_path / f"mod{n}.py").write_text(IMPORTADOR.format(n=n), encoding="utf-8")
    return tmp_path


def test_arquivo_mais_referenciado_vem_primeiro(tmp_path):
    saida = rm.repo_map(_repo(tmp_path))
    linhas = saida.splitlines()
    assert linhas[0].startswith("repo_map (")
    assert linhas[1] == "core.py:"
    assert "alpha" in saida
    # Os importadores só se citam a si mesmos: entram depois do core.
    assert saida.index("core.py:") < saida.index("mod1.py:")


def test_orcamento_corta_e_segunda_chamada_e_deterministica(tmp_path):
    ws = _repo(tmp_path)
    largo = rm.repo_map(ws, budget_tokens=1000)
    curto = rm.repo_map(ws, budget_tokens=8)
    assert len(curto) < len(largo)
    assert len(curto) <= 8 * rm.CHARS_POR_TOKEN
    assert "alpha" in curto  # o corte tira o periférico, não o topo
    # Segunda chamada idêntica sai do cache e tem que ser byte a byte igual.
    assert rm.cache_path(ws).exists()
    assert rm.repo_map(ws, budget_tokens=8) == curto


def test_indice_vazio(tmp_path):
    assert rm.repo_map(tmp_path) == rm.VAZIO
