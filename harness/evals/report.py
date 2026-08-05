"""EVALUATION.md: o exame congelado em markdown, para o humano conferir.

Derivado por definição — tudo aqui já está no `manifest.json` e no
`cases.jsonl`. Existe porque a prova precisa ser lida por quem não vai abrir
JSON: qual versão do exame está valendo, quando congelou, e quais casos ela
tem.

Em D1 a coluna `score` é `—`: congelar o exame e MEDIR são passos separados de
propósito, e um score inventado agora seria pior que nenhum. A coluna existe
desde já para o diff de D2 ser só o preenchimento.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.evals.bundle import evaluation_path, load_cases, manifest_path
from harness.evals.freeze import NOT_FROZEN, Manifest, load_manifest

NO_DATA = "—"
NOT_SCORED = "not scored (D1)"
SHA_WIDTH = 12


def render_evaluation_md(artifact: str, root: Path | None = None) -> str:
    """O markdown do bundle. Sem manifest não há relatório: `ValueError`.

    Relatar exame não congelado seria relatar uma foto que ninguém tirou — e o
    ponto do arquivo é ser citável ("a v2 mediu isto"). Fecha.
    """
    if not manifest_path(artifact, root).is_file():
        raise ValueError(f"{NOT_FROZEN}: {artifact}")
    m = load_manifest(artifact, root)
    if m is None:
        raise ValueError(f"eval:corrupt-manifest: {artifact}")

    lines = [
        f"# EVALUATION — {m.artifact}",
        "",
        f"Exame congelado em `{_rel(artifact, root)}` · v{m.version} · "
        f"{m.case_count} casos · artefato `{m.artifact_sha256[:SHA_WIDTH]}`.",
        "",
        "## versões",
        "",
        "| version | frozen_at | case_count | bundle_sha256 | note |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [_version_row(v) for v in _versions(m)]
    lines += [
        "",
        "## casos",
        "",
        "| id | kind | weight | trials | score | reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {c.id} | {c.kind} | {c.weight:g} | {c.trials} | {NO_DATA} | {NOT_SCORED} |"
        for c in load_cases(artifact, root)
    ]
    return "\n".join(lines) + "\n"


def write_evaluation_md(artifact: str, root: Path | None = None) -> Path:
    """Escreve o `EVALUATION.md` no bundle e devolve o path.

    Escrever DENTRO do bundle é seguro porque o arquivo é derivado e está fora
    do hash (ver `bundle.DERIVED_FILES`): gerar relatório não conta como
    adulterar o exame.
    """
    path = evaluation_path(artifact, root)
    text = render_evaluation_md(artifact, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _versions(m: Manifest) -> list[dict[str, Any]]:
    """Corrente primeiro, histórico descendo — quem lê quer a de hoje."""
    return [
        {
            "version": m.version,
            "bundle_sha256": m.bundle_sha256,
            "frozen_at": m.frozen_at,
            "note": m.note,
            "case_count": m.case_count,
        },
        *sorted(m.history, key=lambda h: h.get("version", 0), reverse=True),
    ]


def _version_row(v: dict[str, Any]) -> str:
    """`case_count` só existe na versão corrente: o registro de histórico é o
    mínimo auditável (version, hash, quando, nota), e inventar a contagem de
    uma versão antiga a partir do bundle de hoje seria mentira."""
    sha = str(v.get("bundle_sha256", ""))[:SHA_WIDTH] or NO_DATA
    count = v.get("case_count", NO_DATA)
    return (
        f"| {v.get('version', NO_DATA)} | {v.get('frozen_at') or NO_DATA} | {count} | "
        f"{sha} | {v.get('note') or NO_DATA} |"
    )


def _rel(artifact: str, root: Path | None) -> str:
    """O bundle em posix, relativo à raiz quando ela é conhecida."""
    path = manifest_path(artifact, root).parent
    if root is not None:
        return path.relative_to(Path(root)).as_posix()
    return path.as_posix()
