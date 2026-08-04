"""Testes do `diff_review`.

O que esta suíte protege são as quatro bordas onde a tool viraria um problema em
vez de uma crítica: workspace sem git (tem que ser SINAL, não exceção — o modelo
não pode travar por não estar num repo), diff grande (teto de chars respeitado e
ordem por churn, senão o arquivo que mais mudou é justo o que fica de fora),
binário (o blob NÃO pode ser despejado no contexto) e arquivo novo (untracked é
onde mora o `.bak` esquecido, e ele não aparece em `git diff`).

Git de verdade em `tmp_path`, sem rede e sem tocar no repo do harness.
"""

import subprocess

import pytest

from harness.backends import review_tools as rt


def git(ws, *args):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(ws),
    }
    return subprocess.run(
        ["git", "-C", str(ws), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "main")
    return ws


def commit(ws, msg="c"):
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", msg)


# --------------------------------------------------------------------------- bordas


def test_sem_git_e_sinal_nao_excecao(tmp_path):
    ws = tmp_path / "solto"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert rt.diff_review(ws) == rt.NADA


def test_repo_limpo_nao_tem_mudanca(repo):
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit(repo)
    assert rt.diff_review(repo) == rt.NADA


# --------------------------------------------------------------------------- volume


def test_50_arquivos_truncado_e_ordenado_por_churn(repo):
    for i in range(50):
        (repo / f"f{i:02d}.py").write_text("linha\n" * 3, encoding="utf-8")
    commit(repo)
    for i in range(50):
        # f00 muda 1 linha, f49 muda 50: a ordem esperada é o inverso do nome.
        (repo / f"f{i:02d}.py").write_text("nova\n" * (i + 1), encoding="utf-8")

    saida = rt.diff_review(repo)
    assert len(saida) <= rt.MAX_CHARS
    assert "truncado" in saida
    assert saida.index("--- f49.py") < saida.index("--- f48.py")
    assert "--- f00.py" not in saida


def test_teto_de_linhas_por_arquivo(repo):
    (repo / "grande.py").write_text("x\n", encoding="utf-8")
    commit(repo)
    (repo / "grande.py").write_text("".join(f"l{i}\n" for i in range(300)), encoding="utf-8")

    saida = rt.diff_review(repo)
    bloco = saida[saida.index("--- grande.py") :]
    assert bloco.count("\n") <= rt.MAX_LINHAS_ARQUIVO + 3
    assert "l299" not in saida


# --------------------------------------------------------------------------- binário


def test_binario_nao_despeja_conteudo(repo):
    blob = repo / "logo.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\xff" * 200)
    commit(repo)
    blob.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xde\xad\xbe\xef" * 500)

    saida = rt.diff_review(repo)
    assert "binário" in saida
    assert f"{blob.stat().st_size} bytes" in saida
    assert "\xde\xad" not in saida
    assert "GIT binary patch" not in saida
    assert len(saida) < 1000  # 2kb de blob não entrou


# --------------------------------------------------------------------------- untracked


def test_untracked_aparece_com_tamanho(repo):
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit(repo)
    (repo / "sobrando.bak").write_text("y" * 42, encoding="utf-8")

    saida = rt.diff_review(repo)
    assert "untracked" in saida
    assert "sobrando.bak (42 bytes)" in saida


def test_untracked_sozinho_ja_e_mudanca(repo):
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit(repo)
    (repo / "nova").mkdir()
    (repo / "nova" / "b.py").write_text("z\n", encoding="utf-8")

    saida = rt.diff_review(repo)
    assert saida != rt.NADA
    assert "nova/b.py" in saida  # -uall: pasta crua não diria nada ao modelo


def test_staged_conta_como_mudanca(repo):
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    commit(repo)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    git(repo, "add", "-A")

    saida = rt.diff_review(repo)
    assert "--- a.py" in saida
    assert "x = 2" in saida


# --------------------------------------------------------------------------- tool


def test_make_review_tools_expoe_uma_tool(repo):
    pytest.importorskip("langchain_core")
    tools = rt.make_review_tools(repo)
    assert [t.name for t in tools] == ["diff_review"]
    assert tools[0].invoke({}) == rt.NADA
