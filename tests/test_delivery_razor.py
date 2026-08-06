"""O razor da entrega não pode deixar lixo de ferramenta virar branch.

`ui-verify.png` é o próprio verificador tirando um screenshot pra si — nunca
é asset de unidade. E um PNG minúsculo (abaixo do piso calibrado no
`uiverify`) é tela em branco, não artefato de verdade. `deliver()` descarta os
dois do staging antes de commitar, e devolve o que descartou porque um
silêncio aqui é pior bug que o vazamento.
"""

import subprocess
from pathlib import Path

from harness.projects import deliver
from harness.uiverify import PNG_MAGIC


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "alvo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _png(path: Path, kb: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    size = int(kb * 1024)
    pad = max(0, size - len(PNG_MAGIC))
    path.write_bytes(PNG_MAGIC + b"\x00" * pad)


def test_screenshot_do_ui_verify_nao_entra_na_entrega(tmp_path):
    repo = _repo(tmp_path)
    (repo / "index.html").write_text("<html></html>\n", encoding="utf-8")
    _png(repo / "ui-verify.png", 40)

    _branch, commit, dropped = deliver(repo, "u1", "r1")

    assert dropped == ("ui-verify.png",)
    assert commit is not None
    shipped = _git(repo, "show", "--name-only", "--pretty=", commit).stdout.split()
    assert "index.html" in shipped
    assert "ui-verify.png" not in shipped
    assert (repo / "ui-verify.png").exists(), "o razor tira do staging, não do disco"


def test_png_em_branco_nao_entra_na_entrega(tmp_path):
    repo = _repo(tmp_path)
    _png(repo / "dist" / "hero.png", 40)
    _png(repo / "dist" / "blank.png", 0.2)

    _branch, commit, dropped = deliver(repo, "u1", "r1")

    assert dropped == ("dist/blank.png",)
    shipped = _git(repo, "show", "--name-only", "--pretty=", commit).stdout.split()
    assert "dist/hero.png" in shipped
    assert "dist/blank.png" not in shipped


def test_arquivo_pequeno_que_nao_e_png_entra(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.js").write_text("1;", encoding="utf-8")

    _branch, commit, dropped = deliver(repo, "u1", "r1")

    assert dropped == ()
    shipped = _git(repo, "show", "--name-only", "--pretty=", commit).stdout.split()
    assert "app.js" in shipped


def test_entrega_so_de_lixo_nao_vira_commit(tmp_path):
    repo = _repo(tmp_path)
    _png(repo / "ui-verify.png", 40)

    branch, commit, dropped = deliver(repo, "u1", "r1")

    assert commit is None
    assert branch == "harness/u1"
    assert dropped != ()


def test_png_ja_versionado_nao_e_descartado(tmp_path):
    repo = _repo(tmp_path)
    _png(repo / "logo.png", 0.2)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "logo")
    (repo / "logo.png").write_bytes(PNG_MAGIC + b"\x01" * 200)  # modificação, ainda pequeno

    _branch, _commit, dropped = deliver(repo, "u1", "r1")

    assert "logo.png" not in dropped
