"""EVALUATION.md: relatório derivado, e derivado NÃO conta como adulteração."""

from __future__ import annotations

from pathlib import Path

from harness.evals import bundle_dir, freeze, verify_frozen
from harness.evals.report import NOT_SCORED, render_evaluation_md, write_evaluation_md

ARTIFACT = "skills/x.md"
CASES = (
    '{"id":"a-1","kind":"code_fix","prompt":"p1","weight":1.0,"trials":4}\n'
    '{"id":"a-2","kind":"refusal","prompt":"p2","axes":["safety"],"weight":2.0,"trials":2}\n'
)


def _tree(root: Path) -> Path:
    art = root / ARTIFACT
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("# skill\n", encoding="utf-8")
    d = bundle_dir(ARTIFACT, root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.jsonl").write_text(CASES, encoding="utf-8")
    return d


def test_evaluation_md_lists_all_versions_and_reasons(tmp_path):
    d = _tree(tmp_path)
    v1 = freeze(ARTIFACT, root=tmp_path, note="baseline")
    (d / "cases.jsonl").write_text(
        CASES + '{"id":"a-3","kind":"diagnosis","prompt":"p3"}\n', encoding="utf-8"
    )
    v2 = freeze(ARTIFACT, root=tmp_path, note="mais um caso")

    text = render_evaluation_md(ARTIFACT, tmp_path)

    # As duas versões, a corrente em cima.
    assert f"| 2 | {v2.frozen_at} | 3 | {v2.bundle_sha256[:12]} | mais um caso |" in text
    assert f"| 1 | {v1.frozen_at} | — | {v1.bundle_sha256[:12]} | baseline |" in text
    assert text.index("| 2 |") < text.index("| 1 |")
    # Um caso por linha, e nenhum score inventado em D1.
    for case_id in ("a-1", "a-2", "a-3"):
        assert f"| {case_id} |" in text
    assert text.count(NOT_SCORED) == 3
    assert "| a-2 | refusal | 2 | 2 | — | not scored (D1) |" in text


def test_evaluation_md_is_excluded_from_manifest_hash(tmp_path):
    _tree(tmp_path)
    v1 = freeze(ARTIFACT, root=tmp_path)

    path = write_evaluation_md(ARTIFACT, tmp_path)

    assert path == bundle_dir(ARTIFACT, tmp_path) / "EVALUATION.md"
    assert path.is_file()
    # Escrever o relatório dentro do bundle não pode virar `unlisted-file`...
    assert verify_frozen(ARTIFACT, root=tmp_path) == []
    # ...nem mudar o hash do exame no próximo freeze.
    v2 = freeze(ARTIFACT, root=tmp_path)
    assert v2.bundle_sha256 == v1.bundle_sha256
    assert "EVALUATION.md" not in v2.files
