"""Política de seleção de ação: o loop aprende QUAL evolução paga.

Bandit determinístico-testável sobre o histórico de mutações do ledger:
score = Wilson lower bound do KEEP-rate por ação + bônus de exploração
(UCB pelas contagens). Ação sem amostra tem bônus infinito — nunca fica
órfã: o loop experimenta antes de julgar. Empate → `rng` do chamador
(seedado → determinístico).

O prior é keyed por `(kind, ação)` quando o chamador sabe o kind do ciclo: qual
evolução paga em `code` não é a que paga em `content`. Célula rala não manda —
`_MIN_CELL` amostras para pesar sozinha, abaixo disso shrinkage linear rumo ao
agregado global, e célula vazia usa o global inteiro. Sem kind (ou sem nada no
global) o comportamento é o de sempre.

O nome da ação vive na coluna `action` de `MutationRow`. Antes dela viajava no
campo livre `note` (`note_with_action` ainda grava lá, e a migração do ledger
faz backfill): `action_of` lê a coluna e cai no note quando ela é NULL. O kind
só existe no `note` (`mutations` não tem coluna `kind` nem `run_id` para juntar
com `runs`): `kind_of` já lê coluna primeiro, para o dia em que ela nascer.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from typing import Any

from harness.ruler.wilson import wilson_interval

# Tokens no `note`: "action=<nome>" e "kind=<kind>", separados por ";" do resto.
ACTION_TAG = "action="
KIND_TAG = "kind="

# Vereditos que contam como tentativa concluída: o experimento aconteceu e a
# régua falou. ABORTED/REJECTED não são evidência sobre a ação — são
# experimento que não aconteceu.
_TRIED = frozenset({"KEEP", "DISCARD", "INCONCLUSIVE"})

# Constante de exploração do UCB (sqrt(2) clássico).
_EXPLORE = math.sqrt(2)

# Amostras que uma célula (kind, ação) precisa para pontuar sozinha. Abaixo
# disso ela é misturada com o agregado global na proporção `n/_MIN_CELL`:
# 3 KEEPs em code não são evidência de que a ação é boa em code.
_MIN_CELL = 5


def note_with_action(action: str | None, note: str | None, kind: str | None = None) -> str | None:
    """Compõe o `note` com os tokens da ação e do kind na frente.

    Sem ação → nota intacta (kind sozinho não interessa: o placar é por ação).
    """
    if not action:
        return note
    tags = [f"{ACTION_TAG}{action}"]
    if kind:
        tags.append(f"{KIND_TAG}{kind}")
    tag = ";".join(tags)
    return f"{tag};{note}" if note else tag


def action_of(row: Any) -> str | None:
    """Nome da ação do registro de mutação, ou None se não dá para saber.

    Coluna `action` primeiro; sem ela (linha antiga, dict de teste), cai no
    token do `note` — as duas eras do ledger respondem a mesma pergunta.
    """
    if field := _get(row, "action"):
        return field
    return _note_tag(row, ACTION_TAG)


def kind_of(row: Any) -> str | None:
    """Kind da tarefa do ciclo que propôs a mutação, ou None se não dá para saber.

    Mesmo padrão do `action_of`: coluna `kind` primeiro — `mutations` não a tem
    hoje, mas dict de teste e um schema futuro respondem igual — e o token do
    `note` como fonte real.
    """
    if field := _get(row, "kind"):
        return field
    return _note_tag(row, KIND_TAG)


def action_stats(history: Iterable[Any], kind: str | None = None) -> dict[str, dict]:
    """Por ação: {"keep": sucessos, "n": tentativas, "rate": p, "lower": Wilson}.

    Pro humano ver o placar do bandit. Só linhas com token de ação e veredito
    concluído entram na conta. `kind` filtra a célula daquele tipo de tarefa —
    é o placar por (kind, ação) que o bandit usa; None = agregado global.
    """
    counts: dict[str, list[int]] = {}
    for row in history:
        name = action_of(row)
        if name is None:
            continue
        if kind is not None and kind_of(row) != kind:
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
    kind: str | None = None,
) -> str:
    """Escolhe a próxima ação de evolução.

    Score = Wilson lower bound do KEEP-rate + bônus UCB
    (`sqrt(2·ln(total+1)/n)`). Ação sem amostra pontua infinito: exploração
    garantida antes de qualquer julgamento. Empate → `rng.choice` sobre os
    empatados na ordem de `action_names` (rng seedado → determinístico).

    `kind` (o tipo da tarefa do ciclo) troca o placar global pelo da célula
    `(kind, ação)`, com shrinkage: célula com `_MIN_CELL`+ amostras pontua
    sozinha, célula rala é misturada ao global na proporção das amostras, e
    célula vazia usa o global puro. Kind None (ou desconhecido no histórico) →
    exatamente o bandit global de antes.
    """
    if not action_names:
        raise ValueError("select_action sem ações: nada a escolher")
    # `explore` (0..1, do governor.explore_budget) fecha a exploração perto do
    # prazo: escala o bônus UCB e, em 0, ação sem amostra deixa de valer inf.
    explore = min(1.0, max(0.0, explore))
    # Materializa: o placar é lido duas vezes (global e célula) e `history`
    # costuma ser um generator do ledger.
    history = list(history)
    stats = action_stats(history)
    total = sum(s["n"] for s in stats.values())
    cell = action_stats(history, kind=kind) if kind else {}
    cell_total = sum(s["n"] for s in cell.values())
    scores = {name: _score(name, stats, total, cell, cell_total, explore) for name in action_names}
    best = max(scores.values())
    tied = [name for name in action_names if scores[name] == best]
    return tied[0] if len(tied) == 1 else rng.choice(tied)


def _score(
    name: str,
    stats: dict[str, dict],
    total: int,
    cell: dict[str, dict],
    cell_total: int,
    explore: float,
) -> float:
    """Score de uma ação: Wilson (com shrinkage da célula) + bônus UCB.

    Sem célula (kind None ou nada gravado naquele kind) o global responde
    sozinho — é o caminho de sempre, inclusive o inf da ação virgem. Célula
    vazia NUNCA zera a ação: o que faltou é evidência daquele kind, não
    evidência.
    """
    glob = stats.get(name)
    c = cell.get(name)
    n_cell = c["n"] if c else 0
    if n_cell == 0:
        if glob is None:
            return math.inf if explore > 0.0 else 0.0
        return glob["lower"] + _bonus(explore, total, glob["n"])
    if n_cell < _MIN_CELL and glob is not None:
        # Célula rala puxada para o agregado: peso `n/_MIN_CELL` na célula, o
        # resto no global (que contém a célula — a mistura é do mesmo KEEP-rate,
        # só menos crédulo). Exploração conta a amostra da célula, que é a que
        # está faltando.
        w = n_cell / _MIN_CELL
        lower = w * c["lower"] + (1.0 - w) * glob["lower"]
    else:
        lower = c["lower"]
    return lower + _bonus(explore, cell_total, n_cell)


def _bonus(explore: float, total: int, n: int) -> float:
    """Bônus UCB do par (amostras da ação, amostras do placar em que ela vive)."""
    return explore * _EXPLORE * math.sqrt(math.log(total + 1) / n)


def _note_tag(row: Any, tag: str) -> str | None:
    """Valor de um token "<tag>=<valor>" no `note`, ou None se não está lá."""
    note = _get(row, "note") or ""
    for part in note.split(";"):
        if part.startswith(tag):
            return part[len(tag) :] or None
    return None


def _get(row: Any, field: str) -> Any:
    """Campo de MutationRow OU dict — o teste não precisa montar a linha inteira."""
    if isinstance(row, dict):
        return row.get(field)
    return getattr(row, field, None)
