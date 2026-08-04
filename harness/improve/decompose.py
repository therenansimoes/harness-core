"""Ação 'decompose': task grande vira FILA de sub-units atômicas.

A lacuna que isto fecha: o harness sabia autorar UMA unit (`harness add`) e
sabia consumir uma fila progressiva (`harness/queue.py`), mas quem quebrava a
task grande em passos era sempre humano — o `u4_dark_mode` só saiu da fronteira
depois de 20 tentativas falhas e de um humano cortá-lo em `u4a/u4b/u4c` à mão.
Enquanto a quebra é humana, o harness não escala em dificuldade, só em volume.

UMA chamada de backend devolve um array JSON de 2..n_max sub-units no MESMO
schema do `add` (`id_slug`, `prompt_md`, `verify_cmd`, `kind`), e cada item
passa pelo `add.parse_author_json` — não por um validador paralelo. Ter uma
segunda rotina de validação de spec aqui é exatamente como as duas divergiriam
sem ninguém notar (mesmo argumento do `redteam` importando `synth._s`).

A ordem é a dependência: as units são gravadas em `projects/<p>/queue/` com
prefixo numérico (`01_<slug>`, `02_<slug>`), porque o driver da fila lê
`sorted(...)` e integra cada aceite antes da próxima — então a sub-unit N pode
assumir o estado que N-1 deixou.

Fail-closed nos dois tempos, como no `redteam`: saída de backend inválida (ou
com menos de 2 units) faz `propose` devolver None, no-op limpo; e o `apply` é
tudo-ou-nada — valida e renderiza o LOTE inteiro antes de tocar o disco, e
desfaz o que criou se a autochecagem final reprovar. Fila meio escrita é pior
que fila nenhuma: o driver rodaria o passo 1 de um plano que não existe.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harness import add
from harness.add import AddError
from harness.backends.registry import get_backend
from harness.types import ExecRequest

if TYPE_CHECKING:
    from harness.improve.target import Action

ACTION = "decompose"

# Teto de passos por decomposição. Pequeno de propósito: plano de 10 passos que
# um humano não revisa é o mesmo problema da task grande, com mais arquivos.
DEFAULT_N_MAX = 5
# Menos que isto não é decomposição — é o `add` com passo extra de custo.
MIN_UNITS = 2

DECOMPOSE_BACKEND = add.ADD_BACKEND
DECOMPOSE_MODEL = add.ADD_MODEL
# Mais folgado que o `add` (ADD_MAX_USD): o plano autora até 5 units numa
# chamada, e mesmo assim é ordens de grandeza mais barato que 20 tentativas.
DECOMPOSE_MAX_USD = 0.60
DECOMPOSE_TIMEOUT_S = 300.0

PROMPT_FILE = add.PROMPT_FILE
UNIT_FILE = add.UNIT_FILE

# Prefixo numérico de fila já existente, nos dois formatos em uso
# (`0004-content-config`, `01_slug`): a próxima decomposição entra DEPOIS.
_PREFIX_RE = re.compile(r"^(\d+)[_-]")


class DecomposeError(RuntimeError):
    """Plano inaplicável: erro claro, nenhuma unit gravada."""


@dataclass(frozen=True)
class SubUnit:
    """Uma sub-unit já validada. `name` é o dir na fila E o `id` da unit — no
    formato das units de projeto escritas à mão (`u4a_botao_tema`), onde o id
    bate com o diretório e o ledger fecha o join sem tradução."""

    index: int
    name: str
    spec: dict[str, str]

    @property
    def slug(self) -> str:
        return self.spec["id_slug"]


@dataclass(frozen=True)
class DecomposeProposal:
    """O plano e sua procedência. Sem `task`/`project` no lote a fila gravada
    não diria de que decisão humana ela veio."""

    task: str
    project: str
    units: tuple[SubUnit, ...]
    backend: str
    queue: Path


def planning_prompt(task: str, project: str, context: str, n_max: int) -> str:
    """As regras que fazem a sub-unit ATÔMICA e não só menor.

    Cada regra existe por uma falha observada: passo longo estoura o contexto do
    modelo local; verify caro não roda a cada tentativa; verify sem mensagem faz
    o executor adivinhar; prompt vago faz modelo 9B improvisar arquitetura.
    """
    return f"""Você DECOMPÕE uma tarefa grande em uma FILA de sub-tarefas atômicas \
para agentes de código pequenos.

Tarefa grande (escrita por um humano): {task}
Projeto: {project}

Contexto do repositório (leia — cite apenas arquivos que existem aqui):
{context}

Regras da decomposição (elas são o trabalho, não decoração):
1. Entre {MIN_UNITS} e {n_max} sub-tarefas, ORDENADAS: a fila é executada em \
ordem e cada aceite é integrado antes da próxima, então a sub-tarefa N PODE \
assumir o estado que N-1 deixou (e deve dizer isso no prompt).
2. Cada sub-tarefa tem que ser resolvível em 1 a 3 minutos por um modelo local \
de ~9B parâmetros. Se um passo exige projetar algo, quebre em mais passos.
3. "prompt_md": instrução LITERAL, passo a passo, em português, citando os \
arquivos reais do contexto e o texto/código exato quando isso for possível. \
Nada de "melhore", "refatore como achar melhor" ou decisão de arquitetura.
4. "verify_cmd": comando shell BARATO (segundos, sem build completo se um grep \
resolve), DETERMINÍSTICO e fail-closed: sai != 0 quando a sub-tarefa não está \
pronta E imprime a mensagem que diz ao executor o que falta. PROIBIDO passo \
manual, revisão humana ou julgamento subjetivo.
5. Cada verify checa SÓ o que sua própria sub-tarefa entrega — verify do passo 3 \
não pode exigir o que o passo 4 vai fazer, senão a fila trava no primeiro passo.

Responda APENAS com um array JSON, sem markdown e sem texto em volta. Cada item:
- "id_slug": slug curto em kebab-case (a-z, 0-9, hífen), único no array
- "prompt_md": a instrução completa em markdown (regra 3)
- "verify_cmd": a régua (regra 4)
- "kind": um de {json.dumps(list(add.KINDS))}"""


def _call_planner(prompt: str, backend: str, model: str | None, max_usd: float) -> str:
    """UMA chamada ao backend. Seam de teste: o fake troca esta função.

    Espelha o `add._call_author` (preflight, workspace descartável, teto de
    custo, resposta lida do trace pelo `add._result_text`) com o nome do backend
    parametrizado — o plano é mais caro que uma unit, mas continua sendo UMA
    chamada, e o texto da resposta é extraído pelo mesmo leitor de trace.
    """
    be = get_backend(backend)
    pre = be.preflight()
    if not pre.ok:
        raise DecomposeError(f"backend {backend} indisponível: {pre.reason}")
    ws = Path(tempfile.mkdtemp(prefix="harness-decompose-"))
    trace = ws / "trace.jsonl"
    try:
        result = be.execute(
            ExecRequest(
                prompt=prompt,
                workspace=ws / "empty",
                tools=("Read",),
                model=model,
                max_turns=1,
                timeout_s=DECOMPOSE_TIMEOUT_S,
                trace_path=trace,
            )
        )
        if not result.ok:
            raise DecomposeError(
                f"chamada de decomposição falhou: exit_reason={result.exit_reason}"
            )
        if result.cost_usd is not None and result.cost_usd > max_usd:
            raise DecomposeError(
                f"decomposição custou ${result.cost_usd:.4f} > teto ${max_usd:.2f} — "
                f"fila não gravada"
            )
        text = add._result_text(trace)
        if not text:
            raise DecomposeError("backend não devolveu texto de resposta")
        return text
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def parse_plan(text: str, n_max: int = DEFAULT_N_MAX) -> list[dict[str, str]]:
    """Array JSON → specs validadas pelo `add`. Qualquer desvio é DecomposeError.

    Cada item volta pelo `add.parse_author_json` (via re-dump): é ele que julga
    slug, prompt vazio, kind e `validate_verify_cmd`. Duplicar aqui a régua de
    verify determinístico seria criar a segunda régua que ninguém sincroniza.
    """
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise DecomposeError(f"resposta sem array JSON: {text[:200]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DecomposeError(f"JSON inválido na resposta: {exc}") from None
    if not isinstance(data, list):
        raise DecomposeError("resposta JSON não é array de sub-tarefas")
    if len(data) < MIN_UNITS:
        raise DecomposeError(
            f"plano com {len(data)} sub-tarefa(s): decompor exige ao menos {MIN_UNITS}"
        )
    if len(data) > n_max:
        # Truncar deixaria a task pela metade sem ninguém avisar; o teto é
        # contrato do prompt, e backend que não o respeitou não é confiável
        # para escolher qual metade fica.
        raise DecomposeError(f"plano com {len(data)} sub-tarefas > teto {n_max}")

    specs: list[dict[str, str]] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise DecomposeError(f"sub-tarefa {i} não é objeto JSON: {item!r}")
        try:
            specs.append(add.parse_author_json(json.dumps(item, ensure_ascii=False)))
        except AddError as exc:
            raise DecomposeError(f"sub-tarefa {i} inválida: {exc}") from None
    slugs = [s["id_slug"] for s in specs]
    dup = sorted({s for s in slugs if slugs.count(s) > 1})
    if dup:
        raise DecomposeError(f"id_slug repetido no plano: {', '.join(dup)}")
    return specs


def queue_dir_for(
    project: str, queue_dir: Path | str | None = None, projects_path: Path | None = None
) -> Path:
    """Fila do projeto pelo registro oficial — não por convenção reinventada."""
    if queue_dir is not None:
        return Path(queue_dir)
    from harness.projects import get_project

    try:
        proj = get_project(project, projects_path)
    except ValueError as exc:
        raise DecomposeError(str(exc)) from None
    return proj.queue_dir or Path("projects") / project / "queue"


def next_index(queue: Path) -> int:
    """Primeiro número livre da fila.

    Começar sempre em 01 colidiria com a decomposição anterior; retomar do maior
    prefixo existente mantém a semântica da fila progressiva: o plano novo entra
    DEPOIS do que já está pendente, que é a ordem em que o driver vai executar.
    """
    highest = 0
    if queue.is_dir():
        for child in queue.iterdir():
            m = _PREFIX_RE.match(child.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def propose_decompose(
    task: str,
    project: str,
    n_max: int = DEFAULT_N_MAX,
    backend: str = DECOMPOSE_BACKEND,
    model: str | None = None,
    max_usd: float = DECOMPOSE_MAX_USD,
    projects_file: Path | None = None,
    queue_dir: Path | str | None = None,
    projects_path: Path | None = None,
    start: int | None = None,
) -> DecomposeProposal | None:
    """Plano de sub-units, ou None.

    None é o contrato do `pick_target`/`propose_redteam`: saída de backend que
    não é um plano válido — ou que "decompôs" em uma única unit — não vira fila
    pela metade. Problema de SETUP (projeto não registrado, backend indisponível,
    teto de custo) levanta DecomposeError: aí não falta gradiente, falta config,
    e engolir isso em None esconderia a causa de quem chamou.
    """
    task = task.strip()
    if not task:
        raise DecomposeError("tarefa vazia")
    if n_max < MIN_UNITS:
        raise DecomposeError(f"n_max={n_max} < mínimo {MIN_UNITS}")
    repo = add.load_project_repo(project, projects_file)
    queue = queue_dir_for(project, queue_dir, projects_path)
    context = add.project_context(repo)
    text = _call_planner(
        planning_prompt(task, project, context, n_max),
        backend,
        model or DECOMPOSE_MODEL,
        max_usd,
    )
    try:
        specs = parse_plan(text, n_max)
    except DecomposeError:
        return None

    first = next_index(queue) if start is None else start
    units = tuple(
        SubUnit(index=first + i, name=f"{first + i:02d}_{spec['id_slug']}", spec=spec)
        for i, spec in enumerate(specs)
    )
    return DecomposeProposal(task=task, project=project, units=units, backend=backend, queue=queue)


def render_unit_toml(unit: SubUnit, proposal: DecomposeProposal) -> str:
    """Formato das units de projeto (com `project`) + `[origin]` da procedência.

    `add._s` em vez de um serializador local pelo motivo do `redteam`: duas
    rotinas string->TOML é como os dois formatos divergem sem ninguém notar.
    """
    total = len(proposal.units)
    return "\n".join(
        [
            "# Autorado por `harness decompose` — passo "
            f"{unit.index - proposal.units[0].index + 1}/{total} de uma fila progressiva.",
            "# A ordem alfabética da fila é a dependência: este passo assume o"
            " estado que o anterior deixou.",
            f"id = {add._s(unit.name)}",
            f"kind = {add._s(unit.spec['kind'])}",
            f"project = {add._s(proposal.project)}",
            f"prompt = {add._s(unit.spec['prompt_md'])}",
            f"verify_cmd = {add._s(unit.spec['verify_cmd'])}",
            "",
            "[origin]",
            'authored_by = "harness decompose"',
            f"project = {add._s(proposal.project)}",
            f"task = {add._s(proposal.task)}",
            f"step = {add._s(f'{unit.index - proposal.units[0].index + 1}/{total}')}",
            "",
        ]
    )


def apply_decompose(
    proposal: DecomposeProposal,
    dry: bool = False,
    out=None,
) -> list[Path]:
    """Renderiza o LOTE, depois escreve. Nessa ordem, e tudo-ou-nada.

    Motivo do `redteam.apply`: fila recusada no meio deixaria o driver rodando
    o passo 1 de um plano incompleto — e o passo 1 PASSA, então o plano quebrado
    entraria no ledger como progresso. Qualquer reprovação (round-trip, dir já
    existente, autochecagem `load_unit`) desfaz o que esta chamada criou.
    """
    out = out if out is not None else sys.stdout
    if len(proposal.units) < MIN_UNITS:
        raise DecomposeError(f"plano com {len(proposal.units)} unit(s): nada a decompor")

    rendered: list[tuple[Path, str, SubUnit]] = []
    for unit in proposal.units:
        text = render_unit_toml(unit, proposal)
        try:
            tomllib.loads(text)  # round-trip: o que sai daqui SEMPRE carrega
        except tomllib.TOMLDecodeError as exc:
            raise DecomposeError(f"unit {unit.name} não é TOML válido: {exc}") from None
        target = proposal.queue / unit.name
        if target.exists():
            raise DecomposeError(f"destino já existe: {target.as_posix()} — nada gravado")
        rendered.append((target, text, unit))

    if dry:
        print(
            f"--dry: nada gravado. Fila seria {proposal.queue.as_posix()} com "
            f"{len(rendered)} passos:",
            file=out,
        )
        for target, text, _unit in rendered:
            print(f"--- {target.as_posix()}/{UNIT_FILE} ---\n{text}", file=out)
        return []

    created: list[Path] = []
    try:
        for target, text, unit in rendered:
            target.mkdir(parents=True)
            created.append(target)
            # unit.toml por ÚLTIMO, motivo do `add`: quem varre a fila só
            # enxerga dir com ele, então falha no meio nunca deixa passo meio
            # escrito elegível para o driver.
            (target / PROMPT_FILE).write_text(unit.spec["prompt_md"] + "\n", encoding="utf-8")
            (target / UNIT_FILE).write_text(text, encoding="utf-8")
        from harness.cli import load_unit  # tardio: cli importa harness.add

        for target in created:
            load_unit(target)  # autochecagem: o que gravamos carrega de verdade
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        for target in created:
            shutil.rmtree(target, ignore_errors=True)
        raise DecomposeError(f"fila revertida ({exc})") from None

    print(
        f"decompose {proposal.project}: {len(created)} passos em "
        f"{proposal.queue.as_posix()} ({', '.join(p.name for p in created)}) "
        f"(rode: harness queue --project {proposal.project})",
        file=out,
    )
    return created


def action() -> Action:
    """A ação registrável — o par propose/apply do registry."""
    from harness.improve.target import Action

    return Action(name=ACTION, propose=propose_decompose, apply=apply_decompose)
