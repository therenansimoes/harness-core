"""Eval-freeze: o exame é imutável, o artefato avaliado não é.

Todo teste monta a árvore num `tmp_path` e passa `root=` explícito. Não é
paranoia de isolamento: sem `root`, `bundle_dir` resolve por `paths.evals_dir()`
— que, rodando a suíte da raiz do checkout, aponta para o `evals/` REAL do repo,
e um teste que congela lá reescreveria o manifest versionado.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import cli, paths
from harness.evals import bundle_dir, freeze, verify_frozen
from harness.evals.bundle import EvalCase, load_cases, manifest_path
from harness.evals.freeze import (
    BUNDLE_MISMATCH,
    CORRUPT_MANIFEST,
    MISSING_FILE,
    MODIFIED,
    NOT_FROZEN,
    UNLISTED_FILE,
    load_manifest,
)
from harness.genome.genome import load as load_genome
from harness.improve import mutate

REPO = Path(__file__).resolve().parents[1]
REAL_GENOME = load_genome(REPO / "config" / "genome.toml")

ARTIFACT = "skills/x.md"
CASES = (
    '{"id":"a-1","kind":"code_fix","prompt":"p1","weight":1.0,"trials":4}\n'
    '{"id":"a-2","kind":"refusal","prompt":"p2","axes":["safety"],"weight":2.0,"trials":2}\n'
)


class _Rule:
    """O mínimo que `mutate.check` olha: o alvo."""

    def __init__(self, target_file: str) -> None:
        self.target_file = target_file


def _tree(root: Path, artifact: str = ARTIFACT, cases: str = CASES) -> Path:
    """Artefato + bundle com casos. Devolve o dir do bundle."""
    art = root / artifact
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("# skill\n", encoding="utf-8")
    d = bundle_dir(artifact, root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.jsonl").write_text(cases, encoding="utf-8")
    return d


def test_bundle_dir_maps_artifact_path_generically(tmp_path):
    assert bundle_dir("skills/python-fixes.md", tmp_path) == tmp_path / "evals/skills/python-fixes"
    assert bundle_dir("workflows/y.toml", tmp_path) == tmp_path / "evals/workflows/y"


def test_freeze_writes_manifest_and_verify_passes(tmp_path):
    _tree(tmp_path)

    m = freeze(ARTIFACT, root=tmp_path, note="primeiro")

    assert manifest_path(ARTIFACT, tmp_path).is_file()
    assert (m.version, m.case_count, m.note) == (1, 2, "primeiro")
    assert m.files == {"cases.jsonl": m.files["cases.jsonl"]}
    assert m.frozen_at.endswith("Z")
    assert verify_frozen(ARTIFACT, root=tmp_path) == []
    assert [c.id for c in load_cases(ARTIFACT, tmp_path)] == ["a-1", "a-2"]
    assert load_cases(ARTIFACT, tmp_path)[1] == EvalCase(
        id="a-2", kind="refusal", prompt="p2", axes=("safety",), weight=2.0, trials=2
    )


def test_verify_detects_modified_case_file(tmp_path):
    d = _tree(tmp_path)
    freeze(ARTIFACT, root=tmp_path)

    with (d / "cases.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"id":"a-3","kind":"code_fix","prompt":"fácil"}\n')

    assert verify_frozen(ARTIFACT, root=tmp_path) == [f"{MODIFIED}:cases.jsonl"]


def test_verify_detects_deleted_case_file(tmp_path):
    d = _tree(tmp_path)
    freeze(ARTIFACT, root=tmp_path)

    (d / "cases.jsonl").unlink()

    assert verify_frozen(ARTIFACT, root=tmp_path) == [f"{MISSING_FILE}:cases.jsonl"]


def test_verify_detects_added_unlisted_file(tmp_path):
    d = _tree(tmp_path)
    freeze(ARTIFACT, root=tmp_path)

    # O ataque que só "arquivo sumiu" não pegaria: ninguém apaga o caso difícil,
    # adiciona o fácil ao lado.
    (d / "cases-easy.jsonl").write_text('{"id":"e","kind":"k","prompt":"p"}\n', encoding="utf-8")

    assert verify_frozen(ARTIFACT, root=tmp_path) == [f"{UNLISTED_FILE}:cases-easy.jsonl"]


def test_verify_detects_bundle_fingerprint_mismatch(tmp_path):
    _tree(tmp_path)
    freeze(ARTIFACT, root=tmp_path)
    path = manifest_path(ARTIFACT, tmp_path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["bundle_sha256"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert verify_frozen(ARTIFACT, root=tmp_path) == [BUNDLE_MISMATCH]


def test_missing_manifest_reports_not_frozen(tmp_path):
    _tree(tmp_path)

    assert verify_frozen(ARTIFACT, root=tmp_path) == [NOT_FROZEN]
    assert load_manifest(ARTIFACT, tmp_path) is None


def test_corrupt_manifest_is_fail_closed(tmp_path):
    _tree(tmp_path)
    freeze(ARTIFACT, root=tmp_path)

    manifest_path(ARTIFACT, tmp_path).write_text('{"version": 1, "files"', encoding="utf-8")

    assert verify_frozen(ARTIFACT, root=tmp_path) == [CORRUPT_MANIFEST]
    assert load_manifest(ARTIFACT, tmp_path) is None


def test_artifact_edit_is_not_a_violation(tmp_path):
    _tree(tmp_path)
    m = freeze(ARTIFACT, root=tmp_path)

    # O tuner reescreve a skill de propósito — é o trabalho dele. Só o exame é
    # imutável.
    (tmp_path / ARTIFACT).write_text("# skill v2, bem melhor\n", encoding="utf-8")

    assert verify_frozen(ARTIFACT, root=tmp_path) == []
    assert load_manifest(ARTIFACT, tmp_path).artifact_sha256 == m.artifact_sha256


def test_refreeze_bumps_version_and_appends_history(tmp_path):
    d = _tree(tmp_path)
    v1 = freeze(ARTIFACT, root=tmp_path, note="v1")

    (d / "cases.jsonl").write_text(
        CASES + '{"id":"a-3","kind":"diagnosis","prompt":"p3"}\n', encoding="utf-8"
    )
    v2 = freeze(ARTIFACT, root=tmp_path, note="v2")

    assert (v2.version, v2.case_count) == (2, 3)
    assert v2.bundle_sha256 != v1.bundle_sha256
    assert v2.history == (
        {
            "version": 1,
            "bundle_sha256": v1.bundle_sha256,
            "frozen_at": v1.frozen_at,
            "note": "v1",
        },
    )
    assert verify_frozen(ARTIFACT, root=tmp_path) == []
    assert load_manifest(ARTIFACT, tmp_path).history == v2.history


def test_genome_cannot_target_eval_bundle(tmp_path):
    """A zona de eval não é calibrável: o genoma real não a declara `mutable`,
    e quem é medido não escreve a prova."""
    alvo = _Rule("evals/skills/python-fixes/cases.jsonl")

    violations = mutate.check(alvo, root=tmp_path, genome=REAL_GENOME)

    assert violations == [f"{mutate.NOT_MUTABLE}:evals/skills/python-fixes/cases.jsonl"]
    # E a skill avaliada continua calibrável — congelar o exame não congela o
    # que ele mede.
    assert mutate.check(_Rule("skills/python-fixes.md"), root=tmp_path, genome=REAL_GENOME) == []


def test_cli_eval_verify_exits_nonzero_on_tamper(tmp_path, monkeypatch, capsys):
    d = _tree(tmp_path)
    monkeypatch.setenv(paths.EVALS_DIR_ENV, str(tmp_path / "evals"))
    monkeypatch.chdir(tmp_path)

    assert cli.main(["eval", "freeze", ARTIFACT, "--note", "cli"]) == 0
    assert cli.main(["eval", "verify", ARTIFACT]) == 0

    (d / "cases.jsonl").write_text('{"id":"a-1","kind":"k","prompt":"trivial"}\n', encoding="utf-8")

    assert cli.main(["eval", "verify", ARTIFACT]) == 1
    assert f"{MODIFIED}:cases.jsonl" in capsys.readouterr().err


def test_torn_case_line_is_value_error(tmp_path):
    d = _tree(tmp_path)

    (d / "cases.jsonl").write_text(CASES + "{não é json}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_cases(ARTIFACT, tmp_path)
