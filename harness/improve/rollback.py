"""Rollback de promoção de tune + auditoria do histórico de um artefato.

A cadeia em `data/tune/<artefato>/` nunca é apagada: rollback move o ponteiro
(`winner.json`) e restaura os bytes de `v{N-1}.txt` no artefato — histórico
append-only continua íntegro para o próximo audit. O evento vira linha do
ledger porque reversão sem registro é experimento que alguém repete amanhã.

Limitações assumidas (adaptação goal→código, não bug):
- Só linhas `action == "tune"` são reversíveis por aqui: mutação de config do
  ciclo improve precisa da regra do catálogo para reconstruir a `Mutation`, e
  o ciclo tem o próprio revert.
- `_persist` sobrescreve a cadeia a cada tune novo, então rollback é LIFO —
  reverter promoção antiga com uma mais nova viva restauraria texto da cadeia
  errada. O guard recusa fora de ordem.
- O rollback restaura os bytes crus de `v{N-1}.txt`. Para a v1 isso é o
  conteúdo original do disco; para versões intermediárias é o texto pontuado
  (sem o re-render do adapter na promoção) — aceitável: é o texto que o exame
  congelado aprovou.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass
from pathlib import Path

from harness.improve import root_dir
from harness.improve.tune import ACTION as TUNE_ACTION
from harness.improve.tune import CHAIN_FILE, PROMOTED, chain_dir
from harness.ledger import store
from harness.types import MutationRow

ACTION = "rollback"
ROLLED_BACK = "rolled_back"
WINNER_FILE = "winner.json"


class RollbackError(Exception):
    """Pré-condição falhou: nada foi escrito, nem no disco nem no ledger."""


@dataclass(frozen=True)
class RollbackRecord:
    artifact: str
    undone_mutation_id: str
    event_id: str
    from_version: int
    to_version: int
    restored_path: str
    recorded_at: str


@dataclass(frozen=True)
class ChainEntry:
    version: int
    overall: float
    reason: str
    valid: bool


@dataclass(frozen=True)
class AuditReport:
    artifact: str
    chain: list[ChainEntry]
    winner: int | None
    winner_source: str
    chain_dir: str
    events: list[MutationRow]


def rollback(
    mutation_id: str,
    *,
    why: str = "manual rollback",
    root: Path | str | None = None,
    who: str | None = None,
) -> RollbackRecord:
    """Desfaz UMA promoção de tune: restaura v(N-1), move o ponteiro, registra.

    Toda pré-condição falha ANTES de qualquer escrita. Depois delas, a ordem é
    deliberada: arquivo restaurado antes do ledger — se a gravação do evento
    morrer no meio, a linha original segue com `reverted=0` e reexecutar o
    mesmo rollback é idempotente no conteúdo.
    """
    row = store.get_mutation(mutation_id)
    if row is None:
        raise RollbackError(f"id desconhecido no ledger: {mutation_id}")
    if row.action != TUNE_ACTION:
        raise RollbackError(
            f"rollback só desfaz promoções de tune; esta linha tem action={row.action!r}"
            " — mutação de config é revertida pelo ciclo improve"
        )
    if row.verdict != PROMOTED:
        raise RollbackError(f"nada a desfazer: verdict={row.verdict!r} não escreveu no disco")
    if row.reverted:
        raise RollbackError(f"já revertida: {mutation_id}")

    prefix = f"{TUNE_ACTION}:"
    if not row.rule_id.startswith(prefix):
        raise RollbackError(f"rule_id fora do padrão tune:<artifact>: {row.rule_id!r}")
    artifact = row.rule_id.removeprefix(prefix)

    from_v, to_v = _ver(row.arm_b), _ver(row.arm_a)
    if from_v is None or to_v is None:
        raise RollbackError(f"braço ilegível na linha do ledger: {row.arm_a!r}/{row.arm_b!r}")

    # Guard LIFO: `store.mutations` devolve mais recente primeiro; tudo que vem
    # ANTES desta linha na lista é promoção mais nova sobre a mesma cadeia.
    for m in store.mutations(rule_id=row.rule_id):
        if m.mutation_id == mutation_id:
            break
        if m.verdict == PROMOTED and not m.reverted:
            raise RollbackError(
                f"há promoção mais nova não revertida ({m.mutation_id}); reverta na ordem LIFO"
            )

    d = chain_dir(artifact)
    src = d / f"v{to_v}.txt"
    if not src.is_file():
        raise RollbackError(f"cadeia sem v{to_v}.txt em {d}")
    try:
        entries = json.loads((d / CHAIN_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RollbackError(f"cadeia sem {CHAIN_FILE} em {d}") from None
    entry = next((e for e in entries if e.get("version") == to_v), None)
    # Restaurável = RETIDA na cadeia monotônica; cadeias legadas (sem o campo
    # `retained`) usavam `valid` nesse papel — mesmo fallback do replay.
    if entry is None or _retained(entry) is not True:
        raise RollbackError(f"v{to_v} não é uma versão válida da cadeia — não restaurável")

    target = root_dir(root) / artifact
    _write_atomic(target, src.read_text(encoding="utf-8"))

    ts = store.now_iso()
    who = who or getpass.getuser()
    _write_atomic(
        d / WINNER_FILE,
        json.dumps(
            {"winner": to_v, "set_at": ts, "by": who, "why": why, "undoes": mutation_id},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )

    event_id = f"{ACTION}:{artifact}@{ts}"
    event = MutationRow(
        mutation_id=event_id,
        rule_id=f"{ACTION}:{artifact}",
        verdict=ROLLED_BACK,
        arm_a=f"v{from_v}",
        arm_b=f"v{to_v}",
        applied_at=ts,
        reverted=False,
        note=f"by={who}; why={why}; undoes={mutation_id}",
        action=ACTION,
    )
    if not store.record_rollback(event, undoes=mutation_id):
        raise RollbackError("evento duplicado no mesmo segundo; tente de novo")

    return RollbackRecord(
        artifact=artifact,
        undone_mutation_id=mutation_id,
        event_id=event_id,
        from_version=from_v,
        to_version=to_v,
        restored_path=str(target),
        recorded_at=ts,
    )


def _retained(row: dict) -> bool:
    """`retained` da linha de chain.json; legado usava `valid` nesse papel."""
    return bool(row.get("retained", row.get("valid", False)))


def audit(artifact: str, *, limit: int = 50) -> AuditReport:
    """Histórico completo do artefato: cadeia + eventos. Vazio é resposta."""
    merged = store.mutations(rule_id=f"{TUNE_ACTION}:{artifact}", limit=None) + store.mutations(
        rule_id=f"{ACTION}:{artifact}", limit=None
    )
    events = sorted(merged, key=lambda m: m.applied_at, reverse=True)[:limit]

    d = chain_dir(artifact)
    chain: list[ChainEntry] = []
    winner: int | None = None
    winner_source = ""
    chain_dir_str = str(d) if d.is_dir() else ""
    if (d / CHAIN_FILE).is_file():
        rows = json.loads((d / CHAIN_FILE).read_text(encoding="utf-8"))
        chain = [
            ChainEntry(
                version=int(r["version"]),
                overall=float(r["overall"]),
                reason=str(r["reason"]),
                valid=bool(r["valid"]),
            )
            for r in rows
        ]
        if (d / WINNER_FILE).is_file():
            winner = int(json.loads((d / WINNER_FILE).read_text(encoding="utf-8"))["winner"])
            winner_source = "winner.json"
        else:
            # Vencedor implícito = última RETIDA (fallback `valid` p/ legado):
            # desde a separação valid/retained, pontuada-mas-descartada é
            # `valid=True` e não pode passar por vencedora.
            winner = max(
                (int(r["version"]) for r in rows if _retained(r)),
                default=None,
            )
            winner_source = "chain.json"

    return AuditReport(
        artifact=artifact,
        chain=chain,
        winner=winner,
        winner_source=winner_source,
        chain_dir=chain_dir_str,
        events=events,
    )


def _ver(s: str) -> int | None:
    """`"v3"` -> 3; qualquer outra coisa -> None (o chamador decide o erro)."""
    if not s.startswith("v"):
        return None
    try:
        return int(s[1:])
    except ValueError:
        return None


def _write_atomic(path: Path, text: str) -> None:
    """tmp irmão + rename: o leitor nunca vê o arquivo meio escrito."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
