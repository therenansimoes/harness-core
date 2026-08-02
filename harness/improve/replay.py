"""Replay: o que sobrou de uma mutação depois que o experimento acabou.

O A/B diz se a mutação ganhou NAQUELA amostra; o replay pergunta outra coisa —
o histórico DEPOIS dela é melhor que o de ANTES? São perguntas diferentes: o
A/B é pareado no tempo (A,B,A,B alternados) e o histórico não. Entre as duas
janelas cabe qualquer outra mudança.

Daí `confounders` ser campo de primeira classe e não nota de rodapé: toda outra
mutação KEEP aplicada no meio das janelas está dentro do delta e não há como
separá-la com o dado que existe. Atribuição honesta NOMEIA o que não consegue
separar, em vez de publicar um número limpo que não é.

Três fatias, não duas: a amostra do PRÓPRIO experimento fica fora das janelas.
Metade dela (o braço A) rodou com a mutação desligada, então contá-la como
"depois" mede uma média de com-e-sem. Quem quer o número do experimento lê
`arm_a`/`arm_b` da linha do ledger, que o replay imprime junto. O corte é por
contagem (as `n_a + n_b` primeiras runs a partir do `applied_at`) porque `runs`
não tem coluna de experimento — run de outro projeto intercalada no meio do A/B
entra na conta e come uma vaga da janela de depois. É o preço conhecido de não
carimbar a run com o `mutation_id`.

Janela filtrada pela chave do experimento — `(kind, tier, backend)`, a mesma do
prior do router — quando as runs do experimento concordam nela. Comparar
"código no tier 0" com "conteúdo no tier 2" seria delta de mistura de trabalho,
não de mutação. Quando não concordam, não há chave e a janela é tudo.

Tempo é comparado como string: o ledger grava ISO-8601 em UTC (`store.now_iso`),
e sem timezone misturado a ordem lexicográfica é a ordem cronológica.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from harness.ledger import store
from harness.ruler.wilson import KEEP, wilson_interval
from harness.types import MutationRow, RunRow

# Chave do experimento: os mesmos três eixos do prior do router.
Key = tuple[str | None, str | None, str | None]
NO_KEY: Key = (None, None, None)


class ReplayError(Exception):
    """Mutação que não está no ledger — não há o que atribuir."""


@dataclass(frozen=True)
class Confounder:
    """Outra mutação KEEP aplicada dentro do intervalo das janelas.

    KEEP porque é a que FICOU no config: DISCARD e INCONCLUSIVE voltaram byte a
    byte e não sobrevivem para explicar delta nenhum.
    """

    mutation_id: str
    rule_id: str
    applied_at: str


@dataclass(frozen=True)
class Attribution:
    """O delta atribuído a uma mutação e tudo que o relativiza.

    `delta` é None quando alguma das janelas está vazia: zero sucessos em zero
    runs não é 0.0, é ausência de amostra — e a régua deste repo não confunde
    as duas coisas (é o mesmo motivo de `wilson_interval(0, 0)` devolver a
    ignorância inteira em vez de um ponto).
    """

    mutation_id: str
    rule_id: str
    verdict: str
    reverted: bool
    succ_before: int
    n_before: int
    succ_after: int
    n_after: int
    ci_before: tuple[float, float]
    ci_after: tuple[float, float]
    delta: float | None
    n_experiment: int = 0
    key: Key = NO_KEY
    confounders: tuple[Confounder, ...] = ()

    @property
    def separated(self) -> bool:
        """Os dois intervalos não se tocam. É a única leitura em que o delta
        pontual não é ruído — mesma regra do `decide_ab`, sem o veredito."""
        return self.ci_after[0] > self.ci_before[1] or self.ci_before[0] > self.ci_after[1]


@dataclass(frozen=True)
class Split:
    """As três fatias do histórico em torno de uma mutação, em ordem crescente."""

    before: tuple[RunRow, ...]
    experiment: tuple[RunRow, ...]
    after: tuple[RunRow, ...]
    key: Key = NO_KEY


def arm_n(text: str) -> int:
    """`"5/6"` -> 6. Formato do ledger; o que não parseia vale 0 tentativa.

    Zero é o valor seguro: sem contagem confiável nada é excluído da janela, e
    uma janela larga demais mostra dado a mais, não a menos.
    """
    _, sep, n_raw = text.partition("/")
    if not sep:
        return 0
    try:
        return max(0, int(n_raw))
    except ValueError:
        return 0


def signature(rows: Sequence[RunRow]) -> Key:
    """`(kind, tier, backend)` quando as runs concordam; None no eixo em que não.

    Sem run nenhuma não há chave: filtrar por uma chave inventada esconderia
    metade do histórico sem dizer por quê.
    """
    if not rows:
        return NO_KEY
    out: list[str | None] = []
    for field in ("kind", "tier", "backend"):
        values = {getattr(r, field) for r in rows}
        out.append(values.pop() if len(values) == 1 else None)
    return (out[0], out[1], out[2])


def split(mutation: MutationRow, runs: Sequence[RunRow]) -> Split:
    """Antes / experimento / depois, já filtradas pela chave do experimento."""
    ordered = sorted(runs, key=lambda r: (r.created_at, r.id or 0))
    before = [r for r in ordered if r.created_at < mutation.applied_at]
    rest = [r for r in ordered if r.created_at >= mutation.applied_at]

    burned = arm_n(mutation.arm_a) + arm_n(mutation.arm_b)
    experiment, after = rest[:burned], rest[burned:]
    key = signature(experiment)
    return Split(
        before=tuple(_matching(before, key)),
        experiment=tuple(experiment),
        after=tuple(_matching(after, key)),
        key=key,
    )


def confounders(
    mutation: MutationRow, mutations: Sequence[MutationRow], span: Split
) -> tuple[Confounder, ...]:
    """Outras mutações KEEP aplicadas dentro do intervalo coberto pelas janelas.

    Fora do intervalo não confunde: a que veio antes da primeira run do "antes"
    está dentro das DUAS janelas (é baseline dos dois lados), e a que veio
    depois da última run do "depois" não tocou em nenhuma delas.
    """
    start = span.before[0].created_at if span.before else mutation.applied_at
    end = span.after[-1].created_at if span.after else mutation.applied_at
    out = [
        Confounder(m.mutation_id, m.rule_id, m.applied_at)
        for m in mutations
        if m.mutation_id != mutation.mutation_id
        and m.verdict == KEEP
        and start <= m.applied_at <= end
    ]
    return tuple(sorted(out, key=lambda c: (c.applied_at, c.mutation_id)))


def attribute(
    mutation_id: str,
    before: Sequence[RunRow],
    after: Sequence[RunRow],
    *,
    mutation: MutationRow | None = None,
    confounded_by: Sequence[Confounder] = (),
    n_experiment: int = 0,
    key: Key = NO_KEY,
) -> Attribution:
    """Delta entre as duas janelas, com IC de Wilson em cada uma.

    Função pura: quem lê o banco é `replay`. `mutation` só traz o rótulo (regra,
    veredito, se voltou) — o número sai das janelas que o chamador montou.
    """
    if mutation is not None and mutation.mutation_id != mutation_id:
        raise ReplayError(
            f"mutação {mutation.mutation_id} não é {mutation_id}"
        )
    succ_before = sum(1 for r in before if r.ok)
    succ_after = sum(1 for r in after if r.ok)
    n_before, n_after = len(before), len(after)
    delta = (
        succ_after / n_after - succ_before / n_before
        if n_before and n_after
        else None
    )
    return Attribution(
        mutation_id=mutation_id,
        rule_id=mutation.rule_id if mutation else "",
        verdict=mutation.verdict if mutation else "",
        reverted=bool(mutation.reverted) if mutation else False,
        succ_before=succ_before,
        n_before=n_before,
        succ_after=succ_after,
        n_after=n_after,
        ci_before=wilson_interval(succ_before, n_before),
        ci_after=wilson_interval(succ_after, n_after),
        delta=delta,
        n_experiment=n_experiment,
        key=key,
        confounders=tuple(confounded_by),
    )


def replay(
    mutation_id: str, path: Path | None = None, limit: int = 2000
) -> Attribution:
    """Atribuição de uma mutação do ledger. `limit` é o teto de runs lidas."""
    rows = store.mutations(limit=limit, path=path)
    mutation = next((m for m in rows if m.mutation_id == mutation_id), None)
    if mutation is None:
        raise ReplayError(f"mutação desconhecida no ledger: {mutation_id!r}")

    span = split(mutation, store.history(limit=limit, path=path))
    return attribute(
        mutation.mutation_id,
        span.before,
        span.after,
        mutation=mutation,
        confounded_by=confounders(mutation, rows, span),
        n_experiment=len(span.experiment),
        key=span.key,
    )


def _matching(rows: Sequence[RunRow], key: Key) -> list[RunRow]:
    kind, tier, backend = key
    return [
        r
        for r in rows
        if (kind is None or r.kind == kind)
        and (tier is None or r.tier == tier)
        and (backend is None or r.backend == backend)
    ]
