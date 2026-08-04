"""Currículo ZPD: qual unidade praticar agora, pela nota que o histórico deu.

Zona de desenvolvimento proximal é a faixa em que a régua INFORMA. Unidade com
score histórico perto de 1.0 já está fechada — repetir só paga o verify. Perto
de 0.0 não há gradiente para subir: a tentativa erra tudo e o log não diz por
onde começar. Entre as duas (`ZONE`) cada tentativa muda a nota, e nota que muda
é a única coisa que o loop consegue seguir.

A nota sai do payload do `verify` no ledger (a régua graduada dos `[checks]`),
média das últimas `K` TENTATIVAS — tentativa, não run: duas tentativas do mesmo
run são duas medições da mesma parede, e é isso que se quer medir. Unidade sem
`[checks]` marca 1.0 fixo e cai fora da zona sozinha, sem caso especial aqui.

Isto NÃO é a ordem default de fila nenhuma: em `projects/<nome>/queue` a ordem
alfabética É a dependência (02 depende do que 01 entregou) e reordenar quebra o
projeto. ZPD é para fila de PRÁTICA — benchmark, exame, drill — onde as unidades
são independentes e a única pergunta é "qual dá mais aprendizado por turno".
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from harness.ledger import store

K = 5                      # tentativas recentes que entram na média por unidade
ZONE = (0.4, 0.8)          # zona de desenvolvimento proximal, fechada nas pontas
MAX_ATTEMPTS_SCAN = 8      # teto de tentativas varridas por run (não há contador)
HISTORY_LIMIT = 200        # janela de runs lida do ledger
DEFAULT_UNITS_DIR = Path("benchmarks/held_in")
UNIT_FILE = "unit.toml"


def unit_scores(
    kind: str | None = None,
    k: int = K,
    db: Path | None = None,
    limit: int = HISTORY_LIMIT,
) -> dict[str, float]:
    """`unit_id -> média das últimas k notas`. Unidade sem nota não aparece.

    A varredura vai do run mais recente para o mais antigo (é a ordem do
    `history`) e, dentro do run, da última tentativa para a primeira: "últimas k"
    tem que significar as k medições mais novas, senão a média fica presa na
    primeira vez que a unidade foi vista.
    """
    out: dict[str, list[float]] = {}
    for row in store.history(kind=kind, limit=limit, path=db):
        got = out.setdefault(row.unit_id, [])
        if len(got) >= k:
            continue
        for score in reversed(_run_scores(row.run_id, db)):
            if len(got) >= k:
                break
            got.append(score)
    return {u: sum(s) / len(s) for u, s in out.items() if s}


def _run_scores(run_id: str, db: Path | None) -> list[float]:
    """Notas das tentativas de um run, em ordem crescente de tentativa.

    Não existe contador de tentativas no ledger: varre-se de 0 até o primeiro
    buraco (com teto). Payload sem `score` é run anterior à régua graduada e não
    entra — 1.0 default aqui empurraria a unidade para fora da zona por engano.
    """
    out: list[float] = []
    for attempt in range(MAX_ATTEMPTS_SCAN):
        ev = store.get_node(run_id, "verify", db, attempt=attempt)
        if ev is None:
            break
        if "score" not in ev:
            continue
        try:
            out.append(float(ev["score"]))
        except (TypeError, ValueError):
            continue
    return out


def next_unit(
    kind: str | None = None,
    units: Sequence[Path] | None = None,
    units_dir: Path | None = None,
    k: int = K,
    db: Path | None = None,
) -> Path | None:
    """A unidade da zona com a nota MAIS ALTA, ou `None` se nenhuma está na zona.

    Mais alta primeiro porque é a mais perto de fechar: entre duas informativas,
    a que precisa de menos turnos para virar accept. Empate resolve por nome —
    escolha instável faria a fila mudar de ordem entre duas leituras iguais.

    `None` não é erro: é "não tenho o que dizer", e o chamador segue com a ordem
    normal dele. É o que mantém o ZPD acessório.
    """
    candidates = _candidates(units, units_dir)
    if not candidates:
        return None
    scores = unit_scores(kind=kind, k=k, db=db)
    lo, hi = ZONE
    inside = [
        (scores[name], name)
        for name in candidates
        if name in scores and lo <= scores[name] <= hi
    ]
    if not inside:
        return None
    best = max(inside, key=lambda pair: (pair[0], _desc(pair[1])))
    return candidates[best[1]]


def order(
    units: Sequence[Path],
    kind: str | None = None,
    k: int = K,
    db: Path | None = None,
) -> list[Path]:
    """As mesmas unidades, com a escolha do ZPD na frente. Sem escolha, intacta.

    Só a primeira posição muda: o resto da ordem é do chamador (na fila de um
    projeto ela é dependência, e mesmo em prática ela é a intenção de quem
    montou a fila). ZPD responde "por onde começar", não "como ordenar tudo".
    """
    pick = next_unit(kind=kind, units=units, k=k, db=db)
    if pick is None:
        return list(units)
    return [pick] + [u for u in units if u != pick]


def _candidates(
    units: Sequence[Path] | None, units_dir: Path | None
) -> dict[str, Path]:
    """`unit_id -> path`. O id é o nome do diretório (é o que o ledger grava)."""
    if units is not None:
        return {p.name: p for p in units}
    base = Path(units_dir) if units_dir is not None else DEFAULT_UNITS_DIR
    if not base.is_dir():
        return {}
    return {
        p.name: p
        for p in sorted(base.iterdir())
        if p.is_dir() and (p / UNIT_FILE).is_file()
    }


def _desc(name: str) -> tuple:
    """Chave que faz o `max` desempatar pelo MENOR nome (ordem alfabética)."""
    return tuple(-ord(c) for c in name)
