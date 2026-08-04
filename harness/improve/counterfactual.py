"""Replay contrafactual: a config de HOJE teria salvado os fracassos de ontem?

Nome `whatif` porque `replay` já é outra coisa neste repo — `improve/replay.py`
atribui delta de histórico a uma mutação, olhando runs que JÁ aconteceram. Aqui
nada é olhado: as unidades que falharam são RE-EXECUTADAS com o genoma atual.

O candidato não é uma mutação isolada, é a config inteira como está agora. Por
isso não há braço A: a pergunta é "o harness de hoje resolve o que o de ontem
não resolveu", e a resposta é uma contagem de unidades, não um intervalo.

Três isolamentos, todos obrigatórios:

- `HARNESS_DATA_DIR` vai para um tmpdir. O ledger real NÃO pode ganhar linhas:
  uma avaliação que grava run muda o prior do router e o denominador do A/B
  seguinte — o contrafactual passaria a influenciar as decisões que ele só
  deveria medir. O workspace sai de graça no mesmo pacote, porque `run_unit`
  monta `ws/` dentro do data dir (`run_graph._provision`), então o tmpdir morre
  levando os worktrees consigo.
- `episodic.disabled()`, mesmo padrão do `exam.py`: a unidade re-rodada falha
  de novo em alguns casos, e gravar essa falha ensinaria a memória global com
  um episódio de avaliação. O juiz não alimenta a memória do avaliado.
- Leitura do ledger ANTES de trocar a env: `store.history` resolve o db por
  `HARNESS_DATA_DIR` na chamada, então inverter a ordem faria o seletor ler o
  banco vazio do tmpdir e devolver zero fracasso.

Unidade que não está mais no disco vira `skipped` em vez de falha: o ledger
guarda o `unit_id`, não a spec, e uma unidade retirada da fila não é evidência
contra a config de hoje. Ela sai do denominador e o relatório NOMEIA quem saiu —
`salvou 2 de 3 (3 de 5 elegíveis)` é honesto, `salvou 2 de 5` não é.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from harness.ledger import store
from harness.types import RunRow

UNIT_FILE = "unit.toml"
PROJECTS_SUBDIR = "projects"
BENCHMARKS_SUBDIR = "benchmarks"
# Estágios da fila de um projeto, na ordem em que a unidade tende a estar: o
# fracasso de ontem normalmente já foi movido para `done`/`stuck`.
QUEUE_STAGES = ("", "done", "stuck")

ENV_DATA_DIR = "HARNESS_DATA_DIR"

# Quantas unidades re-rodar. Baixo de propósito: cada caso é um run completo
# com backend real, então o default é uma dose que cabe num ciclo.
DEFAULT_LIMIT = 5
# Teto de linhas lidas do ledger para achar essas unidades. Alto porque a
# proporção de fracassos é pequena e o corte real é o `limit`.
DEFAULT_SCAN = 500
DEFAULT_REPEAT = 1
MOCK_BACKEND = "mock"

HONESTY = (
    "amostra única, estocástico, não substitui A/B — repetição maior mede "
    "quanto disso é a config e quanto é sorte."
)


@dataclass(frozen=True)
class Case:
    """Uma unidade que falhou no ledger, re-rodada (ou não) com a config atual.

    `unit_dir=None` é o skipped: unidade que não existe mais no disco. `saved`
    é None nesse caso e não conta em nenhum lado da fração — ausência de spec
    não é evidência a favor nem contra.
    """

    unit_id: str
    run_id: str
    kind: str | None
    unit_dir: Path | None
    accepted: int = 0
    repeat: int = 0
    error: str = ""

    @property
    def skipped(self) -> bool:
        return self.unit_dir is None

    @property
    def saved(self) -> bool | None:
        """Salvou = TODA repetição aceitou. Fail-closed como o exame selado:
        com `repeat>1`, unidade que aceita numa e falha na outra não está
        resolvida, está intermitente — e intermitente contado como vitória
        infla o número que o relatório existe para não inflar."""
        if self.skipped or self.repeat == 0:
            return None
        return self.accepted == self.repeat


@dataclass(frozen=True)
class Report:
    """Contagem + os casos, para o humano ver quem foi salvo e quem não."""

    cases: tuple[Case, ...] = ()
    repeat: int = DEFAULT_REPEAT
    backend: str = MOCK_BACKEND
    kind: str | None = None

    @property
    def requested(self) -> int:
        return len(self.cases)

    @property
    def eligible(self) -> tuple[Case, ...]:
        return tuple(c for c in self.cases if not c.skipped)

    @property
    def skipped(self) -> tuple[Case, ...]:
        return tuple(c for c in self.cases if c.skipped)

    @property
    def rescued(self) -> int:
        return sum(1 for c in self.eligible if c.saved)


def failed_runs(
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    scan: int = DEFAULT_SCAN,
    path: Path | None = None,
) -> list[RunRow]:
    """Runs `ok=0` mais recentes, uma por `unit_id`, no máximo `limit`.

    Dedupe por unidade porque re-rodar cinco vezes a mesma unidade com a MESMA
    config não é cinco evidências, é uma medida repetida — e ela infla os dois
    lados da fração ao mesmo tempo. Quem quer repetição pede `repeat`, que é
    explícito e aparece no relatório.
    """
    seen: set[str] = set()
    out: list[RunRow] = []
    for row in store.history(kind=kind, limit=scan, path=path):
        if row.ok or row.unit_id in seen:
            continue
        seen.add(row.unit_id)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _candidates(root: Path, run: RunRow) -> Iterator[Path]:
    """Onde a unidade pode estar, na ordem de busca.

    Com `project` no ledger a busca é direta; sem ele varre os projetos, porque
    unidade de benchmark roda sem projeto e unidade antiga pode ter rodado
    antes da coluna existir.
    """
    projects_root = root / PROJECTS_SUBDIR
    if run.project:
        projects = [projects_root / run.project]
    else:
        projects = sorted(p for p in projects_root.glob("*") if p.is_dir())
    for proj in projects:
        for stage in QUEUE_STAGES:
            queue = proj / "queue"
            yield (queue / stage / run.unit_id) if stage else (queue / run.unit_id)
    for bench in sorted(p for p in (root / BENCHMARKS_SUBDIR).glob("*") if p.is_dir()):
        yield bench / run.unit_id


def resolve_unit(run: RunRow, root: Path | None = None) -> Path | None:
    """Dir da unidade do ledger, ou None se ela não está mais no disco."""
    base = Path(root) if root is not None else Path(".")
    for candidate in _candidates(base, run):
        if (candidate / UNIT_FILE).is_file():
            return candidate
    return None


@contextmanager
def _isolated_data_dir() -> Iterator[Path]:
    """Data dir descartável na env, restaurado no finally.

    Env var e não argumento porque o alcance é maior que `run_unit`: memória,
    `ws/` e o setup de projeto resolvem o data dir por conta própria, fundo no
    grafo. Restaurar inclui a AUSÊNCIA da env — deixar `HARNESS_DATA_DIR`
    apontando para um tmpdir apagado quebraria todo comando seguinte do mesmo
    processo (o autopilot chama isto in-process).
    """
    tmp = Path(tempfile.mkdtemp(prefix="whatif-"))
    previous = os.environ.get(ENV_DATA_DIR)
    os.environ[ENV_DATA_DIR] = str(tmp)
    try:
        yield tmp
    finally:
        if previous is None:
            os.environ.pop(ENV_DATA_DIR, None)
        else:
            os.environ[ENV_DATA_DIR] = previous
        shutil.rmtree(tmp, ignore_errors=True)


def _rerun(
    unit_dir: Path, backend: str, model: str | None, data_dir: Path, repeat: int
) -> tuple[int, str]:
    """Roda a unidade `repeat` vezes. Devolve (aceites, erro da última exceção).

    Exceção conta como não-aceite e não interrompe as outras repetições nem os
    outros casos: unidade que explode é informação sobre a config atual, e o
    relatório precisa chegar inteiro ao humano.
    """
    from harness.graph.run_graph import run_unit

    accepted, error = 0, ""
    for _ in range(repeat):
        # thread_id único: sem isto a segunda repetição RETOMA o checkpoint da
        # primeira e devolve o mesmo veredito sem executar nada.
        thread_id = f"whatif-{unit_dir.name}-{uuid.uuid4().hex[:8]}"
        try:
            state = run_unit(unit_dir, backend, model or None, data_dir, thread_id)
        except Exception as exc:  # noqa: BLE001 - caso que explode é dado, não crash
            error = f"{type(exc).__name__}: {exc}"
            continue
        decision = state.get("decision")
        if decision is not None and decision.action == "accept":
            accepted += 1
    return accepted, error


def whatif(
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    backend: str = MOCK_BACKEND,
    model: str | None = None,
    repeat: int = DEFAULT_REPEAT,
    root: Path | str | None = None,
    ledger_path: Path | None = None,
) -> Report:
    """Re-roda os fracassos do ledger com a config atual e conta os salvos.

    `root` é a raiz onde `projects/` e `benchmarks/` vivem (default cwd, mesma
    convenção cwd-relativa de `exam.SEALED_DIR`); `ledger_path` só existe para
    teste. `repeat<1` é normalizado para 1 — pedir zero execução e receber um
    relatório de zero salvos seria uma resposta sem sentido.
    """
    from harness.memory import episodic

    repeat = max(1, repeat)
    # Ledger lido ANTES do isolamento: depois dele o db é o tmpdir vazio.
    runs = failed_runs(kind=kind, limit=limit, path=ledger_path)
    resolved = [(run, resolve_unit(run, root)) for run in runs]

    cases: list[Case] = []
    with _isolated_data_dir() as data_dir, episodic.disabled():
        for run, unit_dir in resolved:
            if unit_dir is None:
                cases.append(Case(run.unit_id, run.run_id, run.kind, None))
                continue
            accepted, error = _rerun(unit_dir, backend, model, data_dir, repeat)
            cases.append(
                Case(
                    unit_id=run.unit_id,
                    run_id=run.run_id,
                    kind=run.kind,
                    unit_dir=unit_dir,
                    accepted=accepted,
                    repeat=repeat,
                    error=error,
                )
            )
    return Report(cases=tuple(cases), repeat=repeat, backend=backend, kind=kind)


def format_report(report: Report) -> str:
    """Relatório em texto: o placar, uma linha por caso, e o que o limita."""
    if not report.cases:
        alvo = f" de kind={report.kind}" if report.kind else ""
        return f"whatif: nenhum fracasso{alvo} no ledger — nada a re-rodar."

    eligible, skipped = report.eligible, report.skipped
    head = (
        f"salvou {report.rescued} de {len(eligible)} "
        f"({len(eligible)} elegíveis de {report.requested} pedidas; "
        f"skipped: {[c.unit_id for c in skipped]})"
    )
    lines = [head]
    for case in report.cases:
        if case.skipped:
            lines.append(f"  {case.unit_id}: skipped (unidade não está no disco)")
            continue
        veredito = "SALVOU" if case.saved else "falhou de novo"
        detalhe = f" {case.accepted}/{case.repeat}" if report.repeat > 1 else ""
        erro = f" [{case.error}]" if case.error else ""
        lines.append(f"  {case.unit_id}: {veredito}{detalhe}{erro}")
    lines.append(f"backend={report.backend} repeat={report.repeat}")
    if report.repeat == 1:
        lines.append(HONESTY)
    return "\n".join(lines)


def run_whatif(
    kind: str | None = None,
    limit: int = DEFAULT_LIMIT,
    backend: str = MOCK_BACKEND,
    model: str | None = None,
    repeat: int = DEFAULT_REPEAT,
) -> Report:
    """`whatif` + relatório no stdout. É o que a CLI chama."""
    report = whatif(kind=kind, limit=limit, backend=backend, model=model, repeat=repeat)
    print(format_report(report))
    return report
