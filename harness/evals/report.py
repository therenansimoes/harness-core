"""EVALUATION.md: o exame congelado em markdown, para o humano conferir.

Derivado por definição — tudo aqui já está no `manifest.json` e no
`cases.jsonl`. Existe porque a prova precisa ser lida por quem não vai abrir
JSON: qual versão do exame está valendo, quando congelou, e quais casos ela
tem.

Sem `scores` a coluna é `—`: congelar o exame e MEDIR são passos separados de
propósito, e um score inventado seria pior que nenhum. Com `scores`, o mesmo
relatório ganha a seção de eixos e a linha de baseline — o arquivo é UM só para
que "a v2 mediu isto" continue apontando para um caminho só.

O placar é POR EIXO, não por caso: o `Aggregate` é a soma ponderada de todos os
trials de todos os casos, e inventar uma linha por caso a partir dele seria
rateio, não medição. Por isso a coluna de caso continua `—` mesmo com nota.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness.evals.bundle import evaluation_path, load_cases, manifest_path
from harness.evals.freeze import NOT_FROZEN, Manifest, load_manifest
from harness.evals.score import Aggregate

NO_DATA = "—"
NOT_SCORED = "not scored (D1)"
SCORED = "agregado em ## scores"
SHA_WIDTH = 12

# Ordem fixa das colunas de baseline: sem artefato, o que estava no disco, o que
# saiu do loop. Ler as três lado a lado é a única forma de separar "a skill
# ajuda" de "o modelo já resolvia".
BASELINE_ARMS = ("none", "draft", "tuned")


def render_evaluation_md(
    artifact: str,
    root: Path | None = None,
    scores: Aggregate | None = None,
    baseline: Mapping[str, Aggregate] | None = None,
) -> str:
    """O markdown do bundle. Sem manifest não há relatório: `ValueError`.

    Relatar exame não congelado seria relatar uma foto que ninguém tirou — e o
    ponto do arquivo é ser citável ("a v2 mediu isto"). Fecha.

    `scores=None` mantém o relatório de exame congelado e não medido; é o que o
    `harness eval report` emite, e é honesto — ele não roda o tuner.
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
    lines += _scores_section(scores, baseline)
    lines += [
        "",
        "## casos",
        "",
        "| id | kind | weight | trials | score | reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    reason = NOT_SCORED if scores is None else SCORED
    lines += [
        f"| {c.id} | {c.kind} | {c.weight:g} | {c.trials} | {NO_DATA} | {reason} |"
        for c in load_cases(artifact, root)
    ]
    return "\n".join(lines) + "\n"


def _scores_section(
    scores: Aggregate | None, baseline: Mapping[str, Aggregate] | None
) -> list[str]:
    """A seção de nota. Vazia quando ninguém mediu — seção com traço em toda
    célula seria ruído com cara de dado."""
    if scores is None:
        return []
    lines = [
        "",
        "## scores",
        "",
        f"Nota da versão: **{scores.overall:.3f}** (média dos limites inferiores de "
        f"Wilson) · {scores.n} trials.",
        "",
        "| eixo | succ | n | lower |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {axis} | {succ} | {n} | {scores.lower.get(axis, 0.0):.3f} |"
        for axis, (succ, n) in sorted(scores.per_axis.items())
    ]
    if baseline:
        cells = " · ".join(
            f"{arm} {baseline[arm].overall:.3f}" for arm in BASELINE_ARMS if arm in baseline
        )
        lines += ["", f"baseline: {cells}"]
    return lines


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
