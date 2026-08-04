"""Gate do plano: o que reprova uma fila ANTES de ela existir no disco.

O RED-FIRST roda comando de verdade num worktree de verdade (o único jeito de
saber se a régua já está verde), então o repo do fixture é um git repo mínimo.
Nenhum backend é chamado: o gate julga um `DecomposeProposal` já montado.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.improve import plan_gate
from harness.improve.decompose import DecomposeProposal, SubUnit
from harness.improve.plan_gate import check_plan, errors

CHECKS = ({"name": "existe", "cmd": "test -f style.css", "weight": 1.0},)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Repo git com README.md JÁ commitado — é o arquivo do caso 'já verde'."""
    if shutil.which("git") is None:
        pytest.skip("git não disponível")
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# site\n", encoding="utf-8")
    (root / "index.html").write_text("<h1>oi</h1>\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def _spec(slug: str, **kw) -> dict:
    spec: dict = {
        "id_slug": slug,
        "prompt_md": f"# {slug}\n\nCrie `style.css`.",
        "verify_cmd": "test -f style.css || { echo falta; exit 1; }",
        "kind": "code",
        "files": ("style.css",),
        "deps": (),
        "checks": CHECKS,
    }
    spec.update(kw)
    return spec


def _proposal(*specs: dict, queue: Path | None = None) -> DecomposeProposal:
    units = tuple(
        SubUnit(index=i + 1, name=f"{i + 1:02d}_{s['id_slug']}", spec=s)
        for i, s in enumerate(specs)
    )
    return DecomposeProposal(
        task="tema escuro",
        project="oficina",
        units=units,
        backend="mock",
        queue=queue or Path("queue"),
    )


def test_plano_ok_passa_limpo(repo):
    plano = _proposal(
        _spec("css-vars"),
        _spec("botao", verify_cmd="grep -q tema index.html", files=("index.html",)),
    )
    assert check_plan(plano, repo) == []


def test_dep_para_a_frente_reprova(repo):
    plano = _proposal(_spec("um", deps=("dois",)), _spec("dois"))
    problems = check_plan(plano, repo)
    assert any("posterior" in p and "01_um" in p for p in problems)
    assert errors(problems)


def test_dep_inexistente_reprova(repo):
    plano = _proposal(_spec("um"), _spec("dois", deps=("fantasma",)))
    problems = check_plan(plano, repo)
    assert any("não existe no plano" in p for p in problems)


def test_files_sobrepostos_sao_so_aviso(repo):
    plano = _proposal(
        _spec("um", files=("style.css",)),
        _spec("dois", verify_cmd="grep -q tema index.html", files=("style.css",)),
    )
    problems = check_plan(plano, repo)
    assert errors(problems) == []  # aviso não reprova
    assert any(p.startswith(plan_gate.WARN) and "style.css" in p for p in problems)


def test_files_sobrepostos_com_dep_nao_avisam(repo):
    plano = _proposal(
        _spec("um", files=("style.css",)),
        _spec(
            "dois",
            verify_cmd="grep -q tema index.html",
            files=("style.css",),
            deps=("um",),
        ),
    )
    assert check_plan(plano, repo) == []


def test_prompt_grande_reprova(repo):
    plano = _proposal(_spec("um", prompt_md="x" * 3001), _spec("dois"))
    problems = check_plan(plano, repo)
    assert any("3001 chars" in p for p in problems)


def test_sem_checks_reprova(repo):
    plano = _proposal(_spec("um", checks=()), _spec("dois"))
    assert any("sem [checks]" in p for p in check_plan(plano, repo))


def test_kind_invalido_reprova(repo):
    plano = _proposal(_spec("um", kind="poesia"), _spec("dois"))
    assert any("kind inválido" in p for p in check_plan(plano, repo))


def test_unidade_ja_verde_reprova(repo):
    """O check que importa: régua que passa no HEAD é no-op ou régua errada."""
    plano = _proposal(_spec("um", verify_cmd="test -f README.md"), _spec("dois"))
    problems = check_plan(plano, repo)
    assert any("JÁ PASSA" in p and "01_um" in p for p in problems)
    assert not any("02_dois" in p for p in errors(problems))  # essa está vermelha


def test_repo_sem_git_tambem_roda_o_red_first(tmp_path, monkeypatch):
    """Sem git não há worktree — o gate cai no tmpdir em vez de pular o check."""
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "plain"
    root.mkdir()
    (root / "README.md").write_text("# site\n", encoding="utf-8")
    plano = _proposal(_spec("um", verify_cmd="test -f README.md"), _spec("dois"))
    assert any("JÁ PASSA" in p for p in check_plan(plano, root))
