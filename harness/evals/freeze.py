"""Eval-freeze: a prova de que o exame não mudou enquanto a nota subia.

O loop reescreve a skill de propósito — é o trabalho dele. O que ele não pode
reescrever é a prova: um tuner que edita `cases.jsonl` até passar mede a
própria criatividade, não a skill. Então o bundle inteiro é hasheado arquivo a
arquivo, o agregado vira `bundle_sha256`, e `verify_frozen` responde a única
pergunta que importa antes de qualquer score: isto ainda é o mesmo exame?

O artefato avaliado é hasheado JUNTO mas NÃO é enforçado — ele muda por
construção. O `artifact_sha256` está no manifest para o report dizer contra
qual versão da skill a nota foi tirada, não para barrar nada.

Contrato de saída igual ao do `tamper.detect`: lista de strings, vazia = ok.
Nada aqui levanta para dizer "adulterado"; adulteração é dado, não exceção.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.evals.bundle import (
    DERIVED_FILES,
    artifact_path,
    bundle_dir,
    load_cases,
    manifest_path,
)

# `_write` é privado do mutate (é detalhe da cirurgia de TOML), mas a garantia
# que ele dá é exatamente a que o manifest precisa — tmp irmão + `os.replace`,
# nunca um arquivo truncado. Reimplementar aqui seria manter duas rotinas de
# escrita atômica e descobrir a divergência no dia do crash.
from harness.improve.mutate import _write

VERSION = 1

NOT_FROZEN = "eval:not-frozen"
CORRUPT_MANIFEST = "eval:corrupt-manifest"
UNLISTED_FILE = "eval:unlisted-file"
MISSING_FILE = "eval:missing-file"
MODIFIED = "eval:modified"
BUNDLE_MISMATCH = "eval:bundle-mismatch"

# Mesma lista do `tamper.immutable_files`: lixo de ferramenta não é conteúdo de
# exame, e um `__pycache__` aparecendo viraria `unlisted-file` eterno.
SKIP_DIRS = frozenset({".git", "__pycache__"})


@dataclass(frozen=True)
class Manifest:
    """O que foi congelado, quando, e o histórico de tudo que já foi."""

    version: int
    artifact: str
    artifact_sha256: str
    bundle_sha256: str
    files: dict[str, str]
    case_count: int
    frozen_at: str
    note: str
    history: tuple[dict[str, Any], ...] = ()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_files(artifact: str, root: Path | None = None) -> dict[str, str]:
    """rel -> sha256 de tudo que existe HOJE no bundle, menos os derivados."""
    base = bundle_dir(artifact, root)
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file() or SKIP_DIRS & set(p.parts):
            continue
        rel = p.relative_to(base).as_posix()
        if rel in DERIVED_FILES:
            continue
        out[rel] = file_sha256(p)
    return out


def bundle_fingerprint(files: dict[str, str]) -> str:
    """SHA-256 do mapa inteiro: cada path e o hash do seu conteúdo, em ordem.

    As quatro linhas são as de `harness/genome/tamper.py:fingerprint`, de
    propósito — mesmo formato, mesma propriedade (muda se um byte mudar, se um
    arquivo sumir ou se um aparecer). Não importamos de lá porque aquela função
    recebe um `Genome` e varre a blocklist; aqui a lista já está no manifest.
    """
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]}\n".encode())
    return h.hexdigest()


def freeze(artifact: str, *, root: Path | None = None, note: str = "") -> Manifest:
    """Congela o bundle e grava o manifest. Recongelar versiona, não sobrescreve.

    O histórico é append-only dentro do próprio manifest: quem lê o EVALUATION
    precisa ver que o exame v1 virou v2 e quando — exame que muda em silêncio
    entre duas medições invalida a comparação sem deixar rastro.
    """
    base = bundle_dir(artifact, root)
    if not base.is_dir():
        raise FileNotFoundError(f"bundle de eval não existe: {base}")
    art = artifact_path(artifact, root)
    if not art.is_file():
        raise FileNotFoundError(f"artefato avaliado não existe: {art}")

    files = bundle_files(artifact, root)
    if not files:
        raise ValueError(f"bundle vazio: {base}")

    prev = load_manifest(artifact, root)
    m = Manifest(
        version=prev.version + 1 if prev else 1,
        artifact=Path(artifact).as_posix(),
        artifact_sha256=file_sha256(art),
        bundle_sha256=bundle_fingerprint(files),
        files=files,
        case_count=len(load_cases(artifact, root)),
        frozen_at=now_iso(),
        note=note,
        history=(*prev.history, _record(prev)) if prev else (),
    )
    _write_atomic(manifest_path(artifact, root), json.dumps(asdict(m), indent=2) + "\n")
    return m


def load_manifest(artifact: str, root: Path | None = None) -> Manifest | None:
    """O manifest gravado, ou `None` quando ausente OU ilegível.

    Um só `None` para os dois casos porque quem chama não decide nada com a
    diferença; quem precisa dela é o `verify_frozen`, e lá o arquivo é testado
    antes de ser lido.
    """
    try:
        raw = json.loads(manifest_path(artifact, root).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    files = raw.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        return None
    history = raw.get("history") or []
    if not isinstance(history, list) or not all(isinstance(h, dict) for h in history):
        return None
    try:
        return Manifest(
            version=int(raw["version"]),
            artifact=str(raw["artifact"]),
            artifact_sha256=str(raw["artifact_sha256"]),
            bundle_sha256=str(raw["bundle_sha256"]),
            files=dict(files),
            case_count=int(raw["case_count"]),
            frozen_at=str(raw["frozen_at"]),
            note=str(raw.get("note", "")),
            history=tuple(history),
        )
    except (KeyError, TypeError, ValueError):
        return None


def verify_frozen(artifact: str, *, root: Path | None = None) -> list[str]:
    """Violações do bundle contra o manifest. Lista vazia = o exame é o mesmo.

    Fecha nos dois sentidos: arquivo listado que sumiu E arquivo que apareceu
    sem estar listado. Só o primeiro seria metade da prova — quem quer inflar a
    nota adiciona um `cases-easy.jsonl`, não apaga o difícil.
    """
    path = manifest_path(artifact, root)
    if not path.is_file():
        return [NOT_FROZEN]
    m = load_manifest(artifact, root)
    if m is None:
        return [CORRUPT_MANIFEST]

    present = bundle_files(artifact, root)
    out: list[str] = []
    for rel in sorted(set(present) | set(m.files)):
        if rel not in m.files:
            out.append(f"{UNLISTED_FILE}:{rel}")
        elif rel not in present:
            out.append(f"{MISSING_FILE}:{rel}")
        elif present[rel] != m.files[rel]:
            out.append(f"{MODIFIED}:{rel}")
    # O agregado é redundante com o mapa — e é essa redundância que pega o
    # manifest editado a mão, onde hash e arquivo batem porque os dois mudaram.
    if bundle_fingerprint(m.files) != m.bundle_sha256:
        out.append(BUNDLE_MISMATCH)
    return out


def now_iso() -> str:
    """UTC com `Z`: o manifest é comparado entre máquinas, fuso local não serve."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _record(m: Manifest) -> dict[str, Any]:
    """A linha de histórico de uma versão: o mínimo para auditar, sem o mapa
    inteiro de arquivos (que só interessa da versão corrente para frente)."""
    return {
        "version": m.version,
        "bundle_sha256": m.bundle_sha256,
        "frozen_at": m.frozen_at,
        "note": m.note,
    }


def _write_atomic(path: Path, text: str) -> None:
    """`mutate._write`, mais o `touch` que o primeiro freeze exige.

    `_write` copia o modo do arquivo existente e por isso não cria arquivo
    nenhum; o bundle nasce sem manifest. O resto da garantia é o que importa:
    manifest truncado por crash leria `corrupt-manifest` para sempre, e o
    bundle bom viraria exame perdido.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    _write(path, text)
