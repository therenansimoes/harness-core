"""Transferência entre projetos: o que o harness aprendeu num repo viaja.

Bundle = tar.gz com `skills/*.md` (attic fica — é lixeira, não aprendizado),
`routing_prior.json` (placar KEEP/DISCARD por ação, o mesmo agregado que
`harness actions` mostra) e `manifest.json` (versão + timestamp injetável,
para o bundle ser byte-determinístico em teste).

Import é conservador em TODA colisão: skill existente não é sobrescrita, prior
existente não é substituído. Bundle vem de outro repo — conteúdo externo nunca
apaga o que este projeto já sabe; colisão é reportada, não resolvida.
"""

from __future__ import annotations

import json
import tarfile
import time
from pathlib import Path

from harness import __version__
from harness.improve import root_dir
from harness.ledger import store

BUNDLE_VERSION = 1
SKILLS_DIR = "skills"
ATTIC_DIR = "attic"        # skills/attic: aposentadas, não viajam
PRIOR_FILE = "routing_prior.json"
MANIFEST_FILE = "manifest.json"
IMPORTED_PRIOR_NAME = "imported_prior.json"


def imported_prior_path() -> Path:
    """`$HARNESS_DATA_DIR/imported_prior.json`, default `data/imported_prior.json`
    relativo ao cwd — mesma resolução do ledger (`store.data_dir()`), e em
    call-time: o teste (ou uma run com data dir isolado) muda a env e isto
    acompanha."""
    return store.data_dir() / IMPORTED_PRIOR_NAME


def routing_prior(db_path: Path | None = None) -> dict[str, dict[str, int]]:
    """KEEP/DISCARD por ação, lido do ledger inteiro (`limit=None`).

    Conta como `cmd_actions`: nome pela coluna `action` com fallback no token
    do `note` (`action_of`), e só os dois vereditos que são evidência sobre a
    ação. Sem ledger o prior é vazio — bundle sem estatística é honesto.
    """
    from harness.improve.policy import action_of
    from harness.ledger import store

    try:
        muts = store.mutations(limit=None, path=db_path)
    except Exception:  # banco ausente/ilegível: prior vazio, não erro
        return {}
    out: dict[str, dict[str, int]] = {}
    for m in muts:
        name = action_of(m)
        if name is None:
            continue
        tally = out.setdefault(name, {"keep": 0, "discard": 0})
        if m.verdict == "KEEP":
            tally["keep"] += 1
        elif m.verdict == "DISCARD":
            tally["discard"] += 1
    return {name: out[name] for name in sorted(out)}


def export_bundle(
    out_path: Path | str,
    root: Path | str | None = None,
    timestamp: str | None = None,
    db_path: Path | None = None,
) -> Path:
    """Escreve o tar.gz do aprendizado transferível e devolve o caminho."""
    base = root_dir(root)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    skills = _exportable_skills(base / SKILLS_DIR)
    prior = routing_prior(db_path)
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "harness_version": __version__,
        "created_at": timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skills": [p.name for p in skills],
        "actions": len(prior),
    }
    with tarfile.open(out, "w:gz") as tar:
        for path in skills:
            tar.add(path, arcname=f"{SKILLS_DIR}/{path.name}")
        _add_json(tar, PRIOR_FILE, prior)
        _add_json(tar, MANIFEST_FILE, manifest)
    return out


def import_bundle(bundle_path: Path | str, root: Path | str | None = None) -> dict:
    """Traz skills e prior do bundle. Devolve {"imported": [...], "skipped": [...]}.

    `skipped` guarda o par (nome, motivo) — quem importa precisa saber se pulou
    por colisão (já tinha) ou por skill malformada (não confiável).
    """
    base = root_dir(root)
    dest = base / SKILLS_DIR
    imported: list[str] = []
    skipped: list[tuple[str, str]] = []
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = _skill_name(member.name)
            if name is None:
                continue
            if not member.isfile():
                skipped.append((name, "nao-arquivo"))
                continue
            fh = tar.extractfile(member)
            raw = fh.read() if fh is not None else b""
            target = dest / name
            if target.exists():
                skipped.append((name, "colisao"))
                continue
            if not _valid_skill(raw):
                skipped.append((name, "frontmatter-invalido"))
                continue
            dest.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            imported.append(name)
        prior = _member_json(tar, PRIOR_FILE)
    merged = _merge_prior(imported_prior_path(), prior)
    return {
        "imported": sorted(imported),
        "skipped": sorted(skipped),
        "prior_actions": len(merged),
    }


def _exportable_skills(root: Path) -> list[Path]:
    """`skills/*.md` da raiz, ordenado. Subdir nenhum entra — attic inclusive."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.md") if p.is_file())


def _skill_name(arcname: str) -> str | None:
    """Nome do .md se o membro é uma skill da raiz do bundle; senão None.

    Filtra path traversal e subdirs (`skills/attic/x.md`) de uma vez: tar de
    terceiro é entrada não confiável.
    """
    parts = Path(arcname).parts
    if len(parts) != 2 or parts[0] != SKILLS_DIR:
        return None
    name = parts[1]
    if not name.endswith(".md") or name in {".", ".."}:
        return None
    return name


def _valid_skill(raw: bytes) -> bool:
    """Vale o que o loader consegue parsear — mesma régua, nenhuma segunda."""
    import tempfile

    from harness.skills.loader import _parse

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.md"
        try:
            probe.write_bytes(raw)
        except OSError:
            return False
        return _parse(probe) is not None


def _merge_prior(path: Path, incoming: dict) -> dict:
    """Merge conservador: chave que já existe no destino fica como está."""
    current: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            current = {}
    for name, tally in incoming.items():
        current.setdefault(name, tally)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return current


def _add_json(tar: tarfile.TarFile, arcname: str, payload: object) -> None:
    import io

    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    info = tarfile.TarInfo(arcname)
    info.size = len(blob)
    tar.addfile(info, io.BytesIO(blob))


def _member_json(tar: tarfile.TarFile, arcname: str) -> dict:
    try:
        fh = tar.extractfile(arcname)
    except KeyError:
        return {}
    if fh is None:
        return {}
    try:
        loaded = json.loads(fh.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
