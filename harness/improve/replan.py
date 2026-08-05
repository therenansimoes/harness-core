"""`harness replan`: a unidade travou — o que fazer com ela, decidido por dado.

Hoje uma unidade em `queue/stuck/` fica parada até um humano olhar. E o humano
faz sempre a mesma triagem: leu o blocker, olhou se a nota andou, e escolheu
entre "isso é comigo", "isso é espera", "isso é grande demais e precisa ser
quebrado". Os três sinais já estão gravados — o blocker tipado
(`backends/blocker_tools`) no payload do nó `execute`, e a nota da régua graduada
no histórico que o ZPD lê. A triagem é automatizável; a decisão de arquitetura
não é, e por isso o quarto caminho é ESCALAR, não improvisar.

O roteamento:
- `needs_user_input`: imprime e para. Não é fracasso, é pedido de decisão — e
  repicar a unidade não inventa a credencial que falta.
- `external_wait`: não repica. O plano está certo, o mundo é que não respondeu.
- `missing_evidence`/`goal_not_met_yet` com nota média abaixo da zona
  (`zpd.ZONE[0]`) em pelo menos 2 tentativas: o passo é grande demais para quem
  está executando. Repica SÓ ele — a fila inteira não está errada, um degrau é.
  Nota DENTRO da zona é o caso oposto: a régua informa, mais uma tentativa ainda
  paga, e quebrar agora joga fora o gradiente — e a unidade volta de `stuck/`
  para a raiz da fila (`unstick`), senão o "rode de novo" seria mentira.

O repique grava com sufixo ALFABÉTICO (`03a_slug`, `03b_slug`): ordena entre
`03_` e `04_` (o `_` vem antes das letras em ASCII), então o driver executa os
sub-passos no lugar exato do passo travado sem renumerar fila nenhuma — e
renumerar quebraria `deps` de todo mundo que já aponta para `04_`.

Duas unidades travadas na MESMA fila não são um degrau alto, é o plano errado;
e repique que o `plan_gate` reprova é o planejador dizendo que não entendeu.
Nos dois casos isto para e manda escalar para o debate/advisor — que este módulo
deliberadamente NÃO implementa.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from harness.improve import zpd
from harness.improve.decompose import (
    DecomposeError,
    apply_decompose,
    propose_decompose,
    queue_dir_for,
)
from harness.ledger import store

# Rotas. Vocabulário fechado pelo mesmo motivo do blocker: quem chama roteia por
# constante, não por substring de mensagem.
ROUTE_USER = "user"
ROUTE_WAIT = "wait"
ROUTE_SPLIT = "split"
ROUTE_RETRY = "retry"
ROUTE_ESCALATE = "escalate"

# Sub-passos do repique. Menor que o teto do decompose: quebrar um degrau que já
# era um passo em 5 partes é admitir que a fila original não valia nada.
REPLAN_N_MAX = 3
# Tentativas mínimas antes de aceitar que o degrau é alto demais. Uma tentativa
# ruim é ruído (modelo frio, timeout, sorte); duas são um padrão.
MIN_ATTEMPTS = 2
# Fila travada por inteiro: a partir daqui o problema é o plano, não o passo.
MAX_STUCK = 2
SUFFIXES = "abcdefghijklmnopqrstuvwxyz"

STAGES = ("stuck", "", "done")
UNIT_FILE = "unit.toml"
EXEC_NODE = "execute"


@dataclasses.dataclass(frozen=True)
class Decision:
    """A rota e o motivo dela. O motivo é o que o humano lê quando discorda."""

    route: str
    reason: str


def decide(blocker: tuple[str, str] | None, scores: list[float]) -> Decision:
    """Rota da unidade travada, a partir do blocker tipado e das notas.

    `blocker=None` é run que parou sem declarar nada (o caso comum antes do
    blocker tipado existir): cai na mesma régua de nota dos dois tipos de
    retry — não saber POR QUE parou não é razão para tratar diferente de saber
    que faltou evidência.
    """
    tipo, detail = blocker if blocker else (None, "")
    if tipo == "needs_user_input":
        return Decision(ROUTE_USER, detail or "o agente pediu decisão humana")
    if tipo == "external_wait":
        return Decision(ROUTE_WAIT, detail or "esperando serviço externo")
    if len(scores) < MIN_ATTEMPTS:
        return Decision(
            ROUTE_RETRY,
            f"só {len(scores)} tentativa(s) com nota — evidência insuficiente para quebrar",
        )
    media = sum(scores) / len(scores)
    if media < zpd.ZONE[0]:
        return Decision(
            ROUTE_SPLIT,
            f"nota média {media:.2f} < {zpd.ZONE[0]} em {len(scores)} tentativas — "
            "o degrau é alto demais",
        )
    return Decision(
        ROUTE_RETRY,
        f"nota média {media:.2f} ainda na zona — a régua informa, mais uma tentativa paga",
    )


def latest_blocker(unit_id: str, db: Path | None = None, limit: int = zpd.HISTORY_LIMIT):
    """`(tipo, detalhe)` do run mais recente da unidade que declarou blocker.

    O tipo vem do payload do nó `execute` (é lá que o `run_graph` grava); o
    DETALHE só existe no sidecar do workspace (`blocker_tools.read_blocker`), que
    sobrevive apenas se o run foi mantido — por isso é opcional, e ausência dele
    nunca cancela a rota.
    """
    from harness.backends.blocker_tools import read_blocker
    from harness.workspace.provision import workspace_path

    for row in store.history(limit=limit, path=db):
        if row.unit_id != unit_id:
            continue
        for attempt in reversed(range(zpd.MAX_ATTEMPTS_SCAN)):
            ev = store.get_node(row.run_id, EXEC_NODE, db, attempt=attempt)
            if ev and ev.get("blocker"):
                sidecar = read_blocker(workspace_path(row.run_id))
                return str(ev["blocker"]), (sidecar[1] if sidecar else "")
    return None


def unit_scores(unit_id: str, db: Path | None = None, k: int = zpd.K) -> list[float]:
    """As últimas `k` notas da unidade, da mais nova para a mais velha.

    Lê pelo `zpd._run_scores`: a régua graduada é a MESMA do currículo, e uma
    segunda varredura aqui divergiria dela na primeira mudança de payload.
    """
    out: list[float] = []
    for row in store.history(limit=zpd.HISTORY_LIMIT, path=db):
        if row.unit_id != unit_id or len(out) >= k:
            continue
        for score in reversed(zpd._run_scores(row.run_id, db)):
            if len(out) >= k:
                break
            out.append(score)
    return out


def find_unit(queue: Path, unit_id: str) -> Path | None:
    """A unidade na fila, em qualquer estágio. `stuck/` primeiro: é onde ela está."""
    for stage in STAGES:
        path = (queue / stage / unit_id) if stage else (queue / unit_id)
        if (path / UNIT_FILE).is_file():
            return path
    return None


def stuck_count(queue: Path) -> int:
    bucket = queue / "stuck"
    if not bucket.is_dir():
        return 0
    return sum(1 for p in bucket.iterdir() if (p / UNIT_FILE).is_file())


def split_names(unit_id: str, slugs: list[str]) -> list[str]:
    """`03_slug` + n sub-passos -> `03a_<slug1>`, `03b_<slug2>`, ...

    O prefixo do original é preservado inteiro: é ele que põe os sub-passos
    entre o passo travado e o próximo sem tocar em mais nada da fila.
    """
    from harness.improve.decompose import _PREFIX_RE

    m = _PREFIX_RE.match(unit_id)
    # Sem prefixo numérico (unit escrita à mão, `u4a_botao`) o id inteiro vira o
    # prefixo: `u4a_botao-a_<slug>` ainda ordena logo depois do original.
    base = m.group(1) if m else f"{unit_id}-"
    if len(slugs) > len(SUFFIXES):
        raise DecomposeError(f"repique com {len(slugs)} sub-passos: sem sufixo para tantos")
    return [f"{base}{SUFFIXES[i]}_{slug}" for i, slug in enumerate(slugs)]


def unstick(path: Path, queue: Path) -> Path | None:
    """Devolve a unidade de `stuck/` para a raiz da fila. `None` quando não moveu.

    Repicar sem isto é conselho vazio: `queue.pending()` não lista `stuck/`, então
    "rode de novo" rodaria a fila SEM a unidade que acabou de ser julgada como
    "mais uma tentativa paga". Colisão de nome na raiz nunca sobrescreve — a
    unidade que já está na fila é a que vale, e apagá-la perderia trabalho.
    """
    if path.parent.name != "stuck":
        return None
    destino = queue / path.name
    if destino.exists():
        return None
    path.rename(destino)
    return destino


def replan(
    project: str,
    unit_id: str,
    queue_dir: Path | str | None = None,
    projects_file: Path | None = None,
    projects_path: Path | None = None,
    db: Path | None = None,
    n_max: int = REPLAN_N_MAX,
    out=None,
    err=None,
) -> int:
    """Triagem da unidade travada. Devolve o exit code do subcomando."""
    from harness.cli import load_unit

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    queue = queue_dir_for(project, queue_dir, projects_path)
    path = find_unit(queue, unit_id)
    if path is None:
        print(f"replan: unidade {unit_id!r} não está em {queue.as_posix()}", file=err)
        return 1

    blocker = latest_blocker(unit_id, db)
    decision = decide(blocker, unit_scores(unit_id, db))
    tipo = blocker[0] if blocker else "nenhum"
    print(f"replan {unit_id}: blocker={tipo} rota={decision.route} — {decision.reason}", file=out)

    if decision.route == ROUTE_USER:
        print("é sua vez: decida e destrave a unidade — nada foi repicado.", file=out)
        return 0
    if decision.route == ROUTE_WAIT:
        print("espera externa: rode a fila de novo mais tarde, sem repicar.", file=out)
        return 0
    if decision.route == ROUTE_RETRY:
        if unstick(path, queue) is not None:
            print(f"replan: {unit_id} voltou de stuck/ para a fila.", file=out)
        print(f"rode de novo: harness queue --project {project}", file=out)
        return 0

    travadas = stuck_count(queue)
    if travadas >= MAX_STUCK:
        _escalate(project, unit_id, f"{travadas} unidades travadas na mesma fila", err)
        return 1

    unit = load_unit(path)
    proposal = propose_decompose(
        unit.prompt,
        project,
        n_max=n_max,
        projects_file=projects_file,
        queue_dir=queue,
        projects_path=projects_path,
        start=1,
        err=err,
    )
    if proposal is None:
        _escalate(project, unit_id, "o repique foi reprovado no gate do plano", err)
        return 1

    names = split_names(unit_id, [u.slug for u in proposal.units])
    proposal = dataclasses.replace(
        proposal,
        units=tuple(
            dataclasses.replace(u, name=name) for u, name in zip(proposal.units, names, strict=True)
        ),
    )
    apply_decompose(proposal, out=out)
    print(
        f"replan: {unit_id} virou {len(names)} sub-passos ({', '.join(names)}); "
        f"o original fica onde está.",
        file=out,
    )
    return 0


def _escalate(project: str, unit_id: str, motivo: str, err) -> None:
    """A quarta rota: parar e chamar quem decide. Sem inventar arquitetura."""
    print(
        f"replan: {motivo} — {unit_id} NÃO foi repicada.\n"
        f"Isto não é um degrau alto, é o plano: escale para revisão de abordagem "
        f"(debate/advisor humano) antes de gastar mais tentativa em {project}.",
        file=err,
    )
