"""Driver da fila de um projeto: consome `projects/<nome>/queue` pelo GRAFO.

Por que existe: `harness run` é inline (`run_once`) e não entra em worktree, e
`harness improve`/`evolve` leem `benchmarks/held_in` — nenhum dos dois consome
a fila do projeto. Este driver é o caminho que ativa o modo projeto: worktree do
repo real em branch efêmera, e no accept a entrega vira `harness/<unit_id>` para
review humano.

Fila progressiva: unidade que não aceita PARA o loop (a próxima depende dela).
Depois do accept a entrega é integrada no branch default do repo (`integrate`),
senão a unidade seguinte sairia de um HEAD sem o trabalho da anterior.
Aceita vai para `queue/done/`, travada vai para `queue/stuck/` — os buckets que
`harness status` conta.

Depois de cada integração os `verify_cmd` das unidades já em `done/` rodam de
novo DENTRO do repo integrado (regressão). Sem isso um conflito semântico — o
merge passa limpo e quebra o que a unidade anterior provou — acumularia
silencioso no projeto. Verify é questão de segundos; o gap não.

Módulo separado de `projects.py` de propósito: aquele é o registro (+ entrega em
branch) e não conhece o grafo; aqui o driver importa `run_graph`, que já importa
`projects` — juntar os dois fecharia ciclo.
"""

from __future__ import annotations

import shutil
import time
import tomllib
from pathlib import Path

from harness.graph.run_graph import run_unit
from harness.improve import zpd
from harness.ledger import store
from harness.projects import (
    QUEUE_DONE,
    QUEUE_STUCK,
    UNIT_FILE,
    IntegrateError,
    Project,
    get_project,
    integrate,
)
from harness.ruler.verify import log_tail, run_log_dir, run_verify
from harness.types import UnitSpec

DEFAULT_PROJECT = "oficina"
DEFAULT_BACKEND = "deepagents"
DEFAULT_MODEL = "openai:qwopus3.5-4b-coder-mtp"
DEFAULT_DEADLINE_S = 3600.0
REGRESSION_TIMEOUT_S = 120.0  # regressão é barata: verify que demora não é régua


def pending(queue: Path) -> list[Path]:
    """Unidades ainda na fila, em ordem de nome (a ordem é a dependência)."""
    return sorted(
        p
        for p in queue.iterdir()
        if p.is_dir() and p.name not in (QUEUE_DONE, QUEUE_STUCK) and (p / UNIT_FILE).is_file()
    )


def _verify_cmd(unit_dir: Path, proj: Project) -> str | None:
    """`verify_cmd` declarado no `unit.toml` da unidade (ou o default do projeto).

    Devolve `None` quando não há régua para rodar: unidade sem `unit.toml` (ou
    com toml ilegível) é pulada com aviso, nunca derruba a fila — regressão é
    guarda-corpo, não motivo novo de travar.
    """
    try:
        data = tomllib.loads((unit_dir / UNIT_FILE).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    cmd = data.get("verify_cmd") or proj.verify_default
    return str(cmd).strip() or None if cmd else None


def regressions(
    proj: Project,
    queue: Path,
    data: Path,
    timeout_s: float = REGRESSION_TIMEOUT_S,
) -> str | None:
    """Re-roda o verify das unidades de `done/` no repo integrado, em ordem.

    Devolve o nome da PRIMEIRA unidade que quebrou (ou `None`, tudo verde). O
    cwd é o repo do projeto, não o workspace: o que interessa é o resultado do
    merge, e é lá que o conflito semântico aparece. Log fora do repo (mesmo
    `run_log_dir` do verify normal) para não sujar a working tree do alvo.
    """
    done = queue / QUEUE_DONE
    if not done.is_dir():
        return None
    for unit_dir in sorted(p for p in done.iterdir() if p.is_dir()):
        cmd = _verify_cmd(unit_dir, proj)
        if not cmd:
            print(f"regression: {unit_dir.name} sem verify_cmd — pulei")
            continue
        unit = UnitSpec(id=unit_dir.name, path=unit_dir, prompt="", verify_cmd=cmd)
        log_dir = run_log_dir(f"regression-{unit_dir.name}-{int(time.time())}", data)
        verdict = run_verify(unit, proj.repo, timeout_s=timeout_s, log_dir=log_dir)
        if verdict.passed:
            print(f"regression: {unit_dir.name} ok ({verdict.sec:.1f}s)")
            continue
        print(
            f"regression: {unit_dir.name} QUEBROU (exit {verdict.exit_code}, "
            f"{verdict.sec:.1f}s) log={verdict.log_path}"
        )
        tail = log_tail(verdict.log_path)
        if tail:
            print(tail)
        return unit_dir.name
    return None


def run_queue(
    project: str = DEFAULT_PROJECT,
    backend: str = DEFAULT_BACKEND,
    model: str | None = DEFAULT_MODEL,
    deadline_s: float = DEFAULT_DEADLINE_S,
    attempts: int | None = None,
    move: bool = True,
    integrate_accepted: bool = True,
    check_regression: bool = True,
    projects_path: Path | None = None,
    use_zpd: bool = False,
) -> int:
    """Roda a fila do projeto até acabar, travar ou estourar o deadline.

    `move=False` é ensaio: roda igual e não mexe na fila. Devolve o exit code.

    `integrate_accepted=False` desliga o merge da entrega no branch default: a
    fila volta a ser uma sequência de branches que não compõem (só faz sentido
    quando o objetivo é justamente inspecionar cada entrega isolada).

    `check_regression=False` desliga a re-rodada dos verifies de `done/` depois
    da integração — só para quando o custo do verify passou a não ser barato.

    `use_zpd=True` põe na frente a unidade com nota histórica na zona de
    desenvolvimento proximal (`improve/zpd`). DESLIGADO por default e por bom
    motivo: nesta fila a ordem de nome É a dependência, e reordenar quebra o
    projeto. Só faz sentido em fila de prática, onde as unidades independem.
    """
    proj = get_project(project, projects_path)
    queue = proj.queue_dir
    if not queue or not queue.is_dir():
        print(f"fila de {project} não existe: {queue}")
        return 1
    data = store.data_dir()
    t0 = time.time()
    fila = pending(queue)
    if use_zpd:
        fila = zpd.order(fila)
        print(f"zpd: ordem {[p.name for p in fila]}")
    for unit_dir in fila:
        gasto = time.time() - t0
        if gasto > deadline_s:
            print(f"deadline {deadline_s:.0f}s estourado em {gasto:.0f}s — para aqui")
            break
        # thread_id com timestamp do loop: retomada é opt-in (mesmo id), e sem
        # isso um segundo loop no mesmo dia reaproveitaria checkpoint antigo.
        thread = f"{project}-{unit_dir.name}-{int(t0)}"
        state = run_unit(
            unit_dir,
            backend,
            model or None,
            data,
            thread_id=thread,
            max_attempts=attempts,
        )
        decision = state["decision"]
        print(f"{unit_dir.name}: {decision.action} — {decision.reason} thread={thread}")
        if not move:
            continue
        # A entrega precisa entrar no branch default antes da próxima unidade: o
        # worktree dela sai do HEAD do repo, então sem merge o trabalho aceito
        # não compõe. Integração que falha derruba a unidade em stuck/ e para a
        # fila — o mesmo tratamento de uma unidade que travou.
        ok = True
        if decision.action == "accept" and integrate_accepted:
            unit_id = getattr(state.get("unit"), "id", None) or unit_dir.name
            try:
                print(integrate(proj, unit_id))
            except (IntegrateError, ValueError) as exc:
                ok = False
                print(f"{unit_dir.name}: integração falhou — {exc}")
            # Merge limpo não é prova de nada: o que as unidades anteriores
            # provaram tem que continuar valendo depois deste merge. Quebrou →
            # a culpa é da recém-integrada (é ela que mudou o repo).
            if ok and check_regression:
                quebrou = regressions(proj, queue, data)
                if quebrou:
                    ok = False
                    print(f"{unit_dir.name}: regression: {quebrou}")
        aceita = decision.action == "accept" and ok
        bucket = queue / (QUEUE_DONE if aceita else QUEUE_STUCK)
        bucket.mkdir(exist_ok=True)
        shutil.move(str(unit_dir), str(bucket / unit_dir.name))
        if not aceita:
            print(f"{unit_dir.name} travou — fila progressiva, o resto depende dela")
            # Só a linha de comando: repicar é decisão de quem lê, nunca automático.
            print(f"repicar: uv run harness replan --project {project} --unit {unit_dir.name}")
            break
    return 0
