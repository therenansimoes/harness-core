"""Atribuição de skills: quem foi injetada em qual run, e se pagou.

Tabela própria `skill_usage` no MESMO `data/runs.sqlite` do ledger — criada
aqui com CREATE TABLE IF NOT EXISTS, sem tocar em `store.py`. O join com a
tabela `runs` (coluna de sucesso: `ok`) é por `run_id`.

Limitação documentada: o `ExecRequest` não tem `run_id` — o backend grava o
`session_id` quando é o que existe. O join só fecha quando o orquestrador usa
o MESMO id como `runs.run_id` e como session do request; run gravado com outro
id cai no braço "without" da skill, diluindo o lift medido, nunca inflando.

Ação `skill_prune`: skill com lift negativo/nulo e amostra suficiente vai para
`skills/attic/` — poda reversível, nunca delete.
"""

from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path

from harness.ledger.store import db_path as default_db_path
from harness.ledger.store import now_iso

ACTION = "skill_prune"
DEFAULT_MIN_TRIALS = 5
Z = 1.96  # intervalo de 95%

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_usage (
    run_id     TEXT NOT NULL,
    skill      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, skill)
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(USAGE_SCHEMA)
    return conn


def record_usage(
    run_or_session_id: str, skill_names: list[str], db_path: Path | None = None
) -> int:
    """Marca as skills injetadas num run/session. Idempotente por (id, skill)."""
    if not run_or_session_id or not skill_names:
        return 0
    ts = now_iso()
    with _connect(db_path) as conn:
        n = 0
        for name in skill_names:
            cur = conn.execute(
                "INSERT OR IGNORE INTO skill_usage (run_id, skill, created_at) VALUES (?, ?, ?)",
                (run_or_session_id, name, ts),
            )
            n += cur.rowcount
        return n


def wilson_low(successes: int, trials: int, z: float = Z) -> float:
    """Limite inferior do intervalo de Wilson. trials=0 => 0.0 (sem evidência)."""
    if trials <= 0:
        return 0.0
    p = successes / trials
    z2 = z * z
    denom = 1 + z2 / trials
    center = p + z2 / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials))
    return (center - margin) / denom


def lift(skill_name: str, db_path: Path | None = None) -> dict:
    """Sucessos/trials das runs COM a skill vs. SEM, e o Wilson-low de cada.

    Uma run conta uma vez por braço mesmo que tenha várias linhas em `runs`
    com o mesmo run_id (agrega por run_id: sucesso = MAX(ok))."""
    with _connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT r.run_id, MAX(r.ok) AS ok, "
                "  MAX(CASE WHEN u.skill IS NOT NULL THEN 1 ELSE 0 END) AS used "
                "FROM runs r "
                "LEFT JOIN skill_usage u ON u.run_id = r.run_id AND u.skill = ? "
                "GROUP BY r.run_id",
                (skill_name,),
            ).fetchall()
        except sqlite3.OperationalError:  # banco novo: `runs` ainda não existe
            rows = []
    w_s = w_t = wo_s = wo_t = 0
    for r in rows:
        if r["used"]:
            w_t += 1
            w_s += int(bool(r["ok"]))
        else:
            wo_t += 1
            wo_s += int(bool(r["ok"]))
    return {
        "with": (w_s, w_t),
        "without": (wo_s, wo_t),
        "wilson_low_with": wilson_low(w_s, w_t),
        "wilson_low_without": wilson_low(wo_s, wo_t),
    }


def prune_candidates(
    db_path: Path | None = None, min_trials: int = DEFAULT_MIN_TRIALS
) -> list[str]:
    """Skills com lift negativo/nulo (Wilson-low com <= sem) e amostra
    suficiente NOS DOIS braços (>= min_trials cada). Ordem alfabética."""
    with _connect(db_path) as conn:
        names = [
            r["skill"]
            for r in conn.execute(
                "SELECT DISTINCT skill FROM skill_usage ORDER BY skill"
            ).fetchall()
        ]
    out: list[str] = []
    for name in names:
        stats = lift(name, db_path)
        (_, w_t), (_, wo_t) = stats["with"], stats["without"]
        if w_t < min_trials or wo_t < min_trials:
            continue
        if stats["wilson_low_with"] <= stats["wilson_low_without"]:
            out.append(name)
    return out


# --------------------------------------------------------------------------- ação


def propose_prune(db_path: Path | None = None, min_trials: int = DEFAULT_MIN_TRIALS) -> list[str]:
    """Candidatos à poda. Lista vazia = sem gradiente, nada a fazer."""
    return prune_candidates(db_path=db_path, min_trials=min_trials)


def apply_prune(names: list[str], root: Path | str | None = None) -> list[str]:
    """Move skills/<name>.md -> skills/attic/<name>.md. Reversível, nunca delete.

    Devolve os paths (relativos ao root) efetivamente movidos; nome sem arquivo
    correspondente é pulado — a evidência do ledger pode ser mais velha que o
    diretório de skills."""
    base = Path(root) if root is not None else Path(".")
    skills_dir = base / "skills"
    attic = skills_dir / "attic"
    moved: list[str] = []
    for name in names:
        src = skills_dir / f"{name}.md"
        if not src.is_file():
            continue
        attic.mkdir(parents=True, exist_ok=True)
        dst = attic / f"{name}.md"
        os.replace(src, dst)
        moved.append(str(dst.relative_to(base)))
    return moved


def action():
    """A ação registrável — consultada por `target.actions()`."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_prune, apply=apply_prune)
