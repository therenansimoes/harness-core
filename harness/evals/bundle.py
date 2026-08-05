"""Onde o exame de um artefato mora, e o que tem dentro dele.

O bundle espelha o path do artefato: `skills/python-fixes.md` é avaliada por
`evals/skills/python-fixes/`. Espelhar em vez de nomear (um `evals/index.toml`
apontando id -> caso) é o que faz o exame ser genérico: skill, workflow e
prompt entram pela mesma regra, e ninguém precisa registrar nada em lugar
nenhum para congelar o primeiro caso.

Dois arquivos do diretório são DERIVADOS — `manifest.json` (a prova) e
`EVALUATION.md` (o relatório). Nenhum dos dois entra no hash do bundle: um é o
hash, e o outro é regenerado a cada report. Escrevê-los não pode contar como
adulteração do exame.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from harness import paths

CASES_FILE = "cases.jsonl"
MANIFEST_FILE = "manifest.json"
EVALUATION_FILE = "EVALUATION.md"
DERIVED_FILES = frozenset({MANIFEST_FILE, EVALUATION_FILE})

# Os eixos da régua, na ordem em que ela pontua. Default do caso porque o eixo
# é propriedade da régua, não do exame: caso que não diz nada é julgado por
# todos, e caso que restringe (um `refusal` só olha safety e clarity) diz.
DEFAULT_AXES = ("structure", "grounding", "safety", "clarity", "coverage")
DEFAULT_TRIALS = 4

_REQUIRED = ("id", "kind", "prompt")


@dataclass(frozen=True)
class EvalCase:
    """Um caso do exame. Frozen: o que congelou não muda em memória também."""

    id: str
    kind: str
    prompt: str
    expect: dict[str, Any] = field(default_factory=dict)
    axes: tuple[str, ...] = DEFAULT_AXES
    weight: float = 1.0
    trials: int = DEFAULT_TRIALS
    verify_cmd: str | None = None


# 1 em cada HOLDOUT_MOD casos vai para o holdout — por hash do id, nunca por
# sorteio: o mesmo bundle produz o mesmo split em qualquer máquina, sempre.
HOLDOUT_MOD = 4


def case_bucket(case_id: str) -> int:
    """Balde determinístico do caso: sha256 do id, módulo `HOLDOUT_MOD`."""
    return int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16) % HOLDOUT_MOD


def split_cases(cases: Sequence[EvalCase]) -> tuple[list[EvalCase], list[EvalCase]]:
    """(train, holdout) determinístico por hash do id — zero aleatoriedade nova.

    O split é uma VISÃO computada sobre os ids: nada muda no formato do bundle
    nem no manifest. Bundle de 1 caso não tem holdout (gate só de treino); com
    2+ casos os dois lados saem garantidamente não-vazios via fix-up.
    """
    if len(cases) < 2:
        return (list(cases), [])
    holdout = [c for c in cases if case_bucket(c.id) == 0]
    train = [c for c in cases if case_bucket(c.id) != 0]
    # Fix-up determinístico: um lado vazio move o caso de hash extremo — a
    # escolha é função só dos ids, então continua estável entre execuções.
    key = lambda c: (hashlib.sha256(c.id.encode()).hexdigest(), c.id)  # noqa: E731
    if not holdout:
        moved = max(train, key=key)
        train = [c for c in train if c.id != moved.id]
        holdout = [moved]
    elif not train:
        moved = min(holdout, key=key)
        holdout = [c for c in holdout if c.id != moved.id]
        train = [moved]
    return (train, holdout)


def bundle_dir(artifact: str, root: Path | None = None) -> Path:
    """`skills/python-fixes.md` -> `<root>/evals/skills/python-fixes`.

    Sem `root`, a raiz é a que `paths.evals_dir()` resolver. A extensão cai
    porque o bundle é do artefato, não do arquivo: renomear `.md` para `.txt`
    não deveria fazer o exame sumir.
    """
    return _evals_root(root) / _artifact_key(artifact)


def artifact_path(artifact: str, root: Path | None = None) -> Path:
    """O artefato avaliado, na mesma árvore do bundle (`evals/` é irmão dele)."""
    base = Path(root) if root is not None else _evals_root(root).parent
    return base / _relative_artifact(artifact)


def manifest_path(artifact: str, root: Path | None = None) -> Path:
    return bundle_dir(artifact, root) / MANIFEST_FILE


def evaluation_path(artifact: str, root: Path | None = None) -> Path:
    return bundle_dir(artifact, root) / EVALUATION_FILE


def load_cases(artifact: str, root: Path | None = None) -> list[EvalCase]:
    """Os casos do bundle. Linha torta é `ValueError`, nunca caso silenciado.

    Fail-closed até no que parece inofensivo: chave desconhecida levanta em vez
    de ser ignorada, porque `trials: 4` escrito `trial: 4` viraria um exame de
    4 trials virando 1 sem ninguém ver.
    """
    path = bundle_dir(artifact, root) / CASES_FILE
    text = path.read_text(encoding="utf-8")
    out: list[EvalCase] = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{n}: JSON inválido ({e})") from e
        out.append(_case(raw, path, n))
    return out


def _case(raw: Any, path: Path, n: int) -> EvalCase:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}:{n}: cada linha é um objeto JSON, não {type(raw).__name__}")
    faltando = [k for k in _REQUIRED if not isinstance(raw.get(k), str) or not raw[k]]
    if faltando:
        raise ValueError(f"{path}:{n}: campo obrigatório ausente ou vazio: {faltando}")
    conhecidos = set(EvalCase.__dataclass_fields__)
    extra = sorted(set(raw) - conhecidos)
    if extra:
        raise ValueError(f"{path}:{n}: campo desconhecido: {extra}")
    try:
        return EvalCase(
            id=raw["id"],
            kind=raw["kind"],
            prompt=raw["prompt"],
            expect=dict(raw.get("expect") or {}),
            axes=tuple(raw.get("axes") or DEFAULT_AXES),
            weight=float(raw.get("weight", 1.0)),
            trials=int(raw.get("trials", DEFAULT_TRIALS)),
            verify_cmd=raw.get("verify_cmd"),
        )
    except (TypeError, ValueError) as e:
        raise ValueError(f"{path}:{n}: campo com tipo inválido ({e})") from e


def _evals_root(root: Path | None) -> Path:
    return Path(root) / paths.EVALS_SUBDIR if root is not None else paths.evals_dir()


def _artifact_key(artifact: str) -> PurePosixPath:
    """Path relativo do artefato sem extensão — a chave do bundle."""
    return PurePosixPath(_relative_artifact(artifact)).with_suffix("")


def _relative_artifact(artifact: str) -> PurePosixPath:
    """Normaliza e recusa o que escaparia da árvore.

    Absoluto e `..` viram erro em vez de virarem um bundle fora de `evals/`:
    o nome do artefato chega da CLI, e um exame gravado em `../..` não é exame
    de ninguém.
    """
    rel = PurePosixPath(Path(artifact).as_posix())
    if not artifact or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"artefato inválido: {artifact!r} (path relativo, sem '..')")
    return rel
