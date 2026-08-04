"""Testes do gerador prompts/tools.d/ -> prompts/tools.md."""

from pathlib import Path

from scripts.build_prompts import HEADER, build, render


def _seed(src_dir: Path) -> None:
    src_dir.mkdir(parents=True, exist_ok=True)
    # gravados fora de ordem de propósito: a ordem final é alfabética
    (src_dir / "90-task.md").write_text("## task\n", encoding="utf-8")
    (src_dir / "00-intro.md").write_text("# Manual\n", encoding="utf-8")
    (src_dir / "15-execute.md").write_text("## execute\n", encoding="utf-8")
    (src_dir / "10-files.md").write_text("## ls\n", encoding="utf-8")


def test_header_e_ordem_alfabetica(tmp_path):
    src_dir = tmp_path / "tools.d"
    out = tmp_path / "tools.md"
    _seed(src_dir)

    build(src_dir, out)
    content = out.read_text(encoding="utf-8")

    assert content.startswith(HEADER)
    assert content == HEADER + "# Manual\n## ls\n## execute\n## task\n"


def test_idempotente(tmp_path):
    src_dir = tmp_path / "tools.d"
    out = tmp_path / "tools.md"
    _seed(src_dir)

    first = build(src_dir, out)
    mtime = out.stat().st_mtime_ns
    second = build(src_dir, out)

    assert first == second == out.read_text(encoding="utf-8")
    assert out.stat().st_mtime_ns == mtime  # não reescreve sem mudança


def test_sem_fragmentos_falha(tmp_path):
    src_dir = tmp_path / "vazio"
    src_dir.mkdir()
    try:
        render(src_dir)
    except SystemExit as exc:
        assert "nenhum fragmento" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("esperava SystemExit para diretório sem *.md")


def test_repo_tools_md_esta_em_sync():
    """O tools.md versionado tem de ser exatamente o render dos fragmentos."""
    repo_root = Path(__file__).resolve().parent.parent
    esperado = render(repo_root / "prompts" / "tools.d")
    atual = (repo_root / "prompts" / "tools.md").read_text(encoding="utf-8")
    assert atual == esperado
