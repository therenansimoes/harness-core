"""Política de seleção de ação: o loop aprende QUAL evolução paga.

Bandit determinístico-testável sobre o histórico de mutações do ledger:
score = Wilson lower bound do KEEP-rate por ação + bônus de exploração
(UCB pelas contagens). Ação sem amostra tem bônus infinito — nunca fica
órfã: o loop experimenta antes de julgar. Empate → `rng` do chamador
(seedado → determinístico).

O nome da ação vive na coluna `action` de `MutationRow`. Antes dela viajava no
campo livre `note` (`note_with_action` ainda grava lá, e a migração do ledger
faz backfill): `action_of` lê a coluna e cai no note quando ela é NULL.
"""

from __future__ import annotations

import math
import random
from typing import Any, Iterable

from harness.ruler.wilson import wilson_interval

# Token no `note`: "action=<nome>", separado por ";" do resto da nota.
ACTION_TAG = "action="

# Vereditos que contam como tentativa concluída: o experimento aconteceu e a
# régua falou. ABORTED/REJECTED não são evidência sobre a ação — são
# experimento que não aconteceu.
_TRIED = frozenset({"KEEP", "DISCARD", "INCONCLUSIVE"})

# Constante de exploração do UCB (sqrt(2) clássico).
_EXPLORE = math.sqrt(2)


def note_with_action(action: str | None, note: str | None) -> str | None:
    """Compõe o `note` com o token da ação na frente. Sem ação → nota intacta."""
    if not action:
        return note
    tag = f"{ACTION_TAG}{action}"
    return f"{tag};{note}" if note else tag


def action_of(row: Any) -> str | None:
    """Nome da ação do registro de mutação, ou None se não dá para saber.

    Coluna `action` primeiro; sem ela (linha antiga, dict de teste), cai no
    token do `note` — as duas eras do ledger respondem a mesma pergunta.
    """
    if (field := _get(row, "action")):
        return field
    note = _get(row, "note") or ""
    for part in note.split(";"):
        if part.startswith(ACTION_TAG):
            return part[len(ACTION_TAG):] or None
    return None


def action_stats(history: Iterable[Any]) -> dict[str, dict]:
    """Por ação: {"keep": sucessos, "n": tentativas, "rate": p, "lower": Wilson}.

    Pro humano ver o placar do bandit. Só linhas com token de ação e veredito
    concluído entram na conta.
    """
    counts: dict[str, list[int]] = {}
    for row in history:
        name = action_of(row)
        if name is None:
            continue
        verdict = _get(row, "verdict")
        if verdict not in _TRIED:
            continue
        c = counts.setdefault(name, [0, 0])
        c[1] += 1
        if verdict == "KEEP":
            c[0] += 1
    out: dict[str, dict] = {}
    for name in sorted(counts):
        succ, n = counts[name]
        lower, _ = wilson_interval(succ, n)
        out[name] = {
            "keep": succ,
            "n": n,
            "rate": succ / n if n else 0.0,
            "lower": lower,
        }
    return out


def select_action(
    action_names: list[str],
    history: Iterable[Any],
    rng: random.Random,
    explore: float = 1.0,
) -> str:
    """Escolhe a próxima ação de evolução.

    Score = Wilson lower bound do KEEP-rate + bônus UCB
    (`sqrt(2·ln(total+1)/n)`). Ação sem amostra pontua infinito: exploração
    garantida antes de qualquer julgamento. Empate → `rng.choice` sobre os
    empatados na ordem de `action_names` (rng seedado → determinístico).
    """
    if not action_names:
        raise ValueError("select_action sem ações: nada a escolher")
    # `explore` (0..1, do governor.explore_budget) fecha a exploração perto do
    # prazo: escala o bônus UCB e, em 0, ação sem amostra deixa de valer inf.
    explore = min(1.0, max(0.0, explore))
    stats = action_stats(history)
    total = sum(s["n"] for s in stats.values())
    scores: dict[str, float] = {}
    for name in action_names:
        n = stats[name]["n"] if name in stats else 0
        if n == 0:
            scores[name] = math.inf if explore > 0.0 else 0.0
        else:
            bonus = explore * _EXPLORE * math.sqrt(math.log(total + 1) / n)
            scores[name] = stats[name]["lower"] + bonus
    best = max(scores.values())
    tied = [name for name in action_names if scores[name] == best]
    return tied[0] if len(tied) == 1 else rng.choice(tied)


def _get(row: Any, field: str) -> Any:
    """Campo de MutationRow OU dict — o teste não precisa montar a linha inteira."""
    if isinstance(row, dict):
        return row.get(field)
    return getattr(row, field, None)
