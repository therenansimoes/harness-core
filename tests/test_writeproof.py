"""Prova-de-escrita contra o baseline do provision, não `git status` cru.

O que estes testes protegem: a worktree isolada nasce suja do PRÓPRIO harness
(`.harness/setup.log` do `ws_setup.ensure`, escrito ANTES do agente) — uma
régua que conta isso como "mudou" aceita run onde o agente não fez nada. O
baseline gravado no instante do provision é a única referência honesta.
"""

import subprocess
from pathlib import Path

from harness import cli
from harness.workspace import writeproof


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def _repo(tmp_path) -> Path:
    """Repo git com um arquivo commitado — o ponto de partida de um provision."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("x\n", encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "inicial")
    return ws


def _simula_provision(ws: Path) -> None:
    """`ws_setup.ensure` escreve `.harness/setup.log` ANTES do baseline —
    exatamente essa ordem é o que impede o lixo do harness de virar prova."""
    scratch = ws / ".harness"
    scratch.mkdir()
    (scratch / "setup.log").write_text("$ npm ci\nrc=0\n", encoding="utf-8")
    writeproof.save_baseline(ws)


def test_baseline_absorve_o_lixo_do_provision_e_reprova_sem_escrita(tmp_path):
    ws = _repo(tmp_path)
    _simula_provision(ws)

    assert writeproof.new_paths(ws) == ()

    rc = cli.main(["proof-of-write", "--ws", str(ws)])

    assert rc == 1


def test_escrita_depois_do_baseline_aceita(tmp_path):
    ws = _repo(tmp_path)
    _simula_provision(ws)

    (ws / "index.html").write_text("<html></html>\n", encoding="utf-8")

    assert writeproof.new_paths(ws) == ("index.html",)
    assert cli.main(["proof-of-write", "--ws", str(ws)]) == 0


def test_sem_baseline_reprova_com_exit_2(tmp_path):
    ws = _repo(tmp_path)
    _simula_provision(ws)
    (ws / ".harness" / "write-baseline.json").unlink()

    assert writeproof.new_paths(ws) is None
    assert cli.main(["proof-of-write", "--ws", str(ws)]) == 2


def test_new_paths_none_fora_de_repo_git(tmp_path):
    ws = tmp_path / "nao-e-repo"
    ws.mkdir()

    assert writeproof.new_paths(ws) is None


def test_dirty_ignora_verificador_selado(tmp_path):
    """`verify.py` materializado ENQUANTO a régua roda não pode provar a si
    mesmo — se contasse, todo run passaria só por o verificador existir."""
    ws = _repo(tmp_path)
    (ws / "verify.py").write_text("# selado\n", encoding="utf-8")

    assert writeproof.dirty(ws) == ()
