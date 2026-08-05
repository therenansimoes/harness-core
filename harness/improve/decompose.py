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

Forma válida não é plano executável: quem julga o PLANO é `plan_gate.check_plan`
(régua, deps para trás, prompt, e o RED-FIRST — toda unidade tem que sair
vermelha no HEAD). Reprovar vale UMA re-chamada com os motivos anexados; a
segunda reprovação devolve None e manda escalar.

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
# Teto de arquivos por sub-tarefa: passo que toca mais que isto não é atômico.
MAX_FILES = 3
# Régua graduada da sub-tarefa: menos de 2 não gradua nada, mais de 3 vira
# verify caro rodando a cada tentativa.
MIN_CHECKS = 2
MAX_CHECKS = 3

DECOMPOSE_BACKEND = add.ADD_BACKEND
DECOMPOSE_MODEL = add.ADD_MODEL
# `--planner local`: o plano sai da frota local, com o adapter de raciocínio de
# `config/adapters.toml`. Planejar barato é o que torna a decomposição rotina —
# mas o gate é o MESMO, porque plano local ruim custa a fila igual.
PLANNERS = ("remote", "local")
LOCAL_PLANNER_BACKEND = "deepagents"
LOCAL_PLANNER_ADAPTER = "reasoning"
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
    # `object` e não `str`: além dos campos do `add`, a spec carrega `files`,
    # `deps` (tuplas de string) e `checks` (tupla de dicts).
    spec: dict[str, object]

    @property
    def slug(self) -> str:
        return str(self.spec["id_slug"])


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
    o executor adivinhar; prompt vago faz modelo pequeno improvisar arquitetura;
    asset com prefixo `dist/` dá 404 no gate de tela, que serve `dist/` como raiz.
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
2. Cada sub-tarefa tem que ser resolvível por um modelo local de 4B parâmetros, \
em no máximo 12 turnos, com "prompt_md" de até 3000 caracteres e instrução \
LITERAL. Se um passo exige projetar algo, quebre em mais passos.
3. "prompt_md": instrução LITERAL, passo a passo, em português, citando os \
arquivos reais do contexto e o texto/código exato quando isso for possível. \
Nada de "melhore", "refatore como achar melhor" ou decisão de arquitetura.
4. "verify_cmd": comando shell BARATO (segundos, sem build completo se um grep \
resolve), DETERMINÍSTICO e fail-closed: sai != 0 quando a sub-tarefa não está \
pronta E imprime a mensagem que diz ao executor o que falta. PROIBIDO passo \
manual, revisão humana ou julgamento subjetivo.
5. Cada verify checa SÓ o que sua própria sub-tarefa entrega — verify do passo 3 \
não pode exigir o que o passo 4 vai fazer, senão a fila trava no primeiro passo.
6. "files": lista dos arquivos que a sub-tarefa TOCA, no máximo {MAX_FILES}, \
caminhos reais do contexto acima. Passo que precisa mexer em mais que \
{MAX_FILES} arquivos não é atômico — quebre em mais passos.
7. "deps": lista de "id_slug" de sub-tarefas ANTERIORES deste mesmo array de que \
esta depende de verdade. Só para trás, nunca para a frente. Vazio ([]) quando a \
ordem da fila já basta.
8. "checks": de {MIN_CHECKS} a {MAX_CHECKS} checagens nomeadas, GRADUADAS por \
dificuldade (a primeira é a mais fácil, a última é a mais próxima do verify_cmd). \
Cada uma é um objeto com "name" (curto, [a-z0-9_-], sem espaço), "cmd" (shell \
barato, determinístico, sai != 0 quando falta) e "weight" (número > 0, maior \
para a checagem mais difícil). Elas dizem QUANTO da tarefa passou; o \
"verify_cmd" continua sendo o que decide pronto/não-pronto.
9. Produto web: `dist/` é a RAIZ servida do site. Dentro dos arquivos de `dist/`, \
todo caminho de asset é relativo a `dist/` — `css/style.css`, `js/app.js`, \
`data/x.json`. NUNCA escreva o prefixo `dist/` em `href`, `src` ou `fetch()`: o \
gate de tela serve `dist/` como raiz e o prefixo vira 404.
10. Sub-tarefa que cria página ou tela EXIGE conteúdo visível renderizado (texto, \
lista ou elemento com dado na tela), não só o arquivo existir — o gate de tela \
reprova página vazia.

Responda APENAS com um array JSON, sem markdown e sem texto em volta. Cada item:
- "id_slug": slug curto em kebab-case (a-z, 0-9, hífen), único no array
- "prompt_md": a instrução completa em markdown (regras 2 e 3)
- "verify_cmd": a régua (regra 4)
- "kind": um de {json.dumps(list(add.KINDS))}
- "files": lista de caminhos (regra 6)
- "deps": lista de id_slug anteriores (regra 7)
- "checks": lista de {{"name", "cmd", "weight"}} (regra 8)"""


def _call_planner(
    prompt: str,
    backend: str,
    model: str | None,
    max_usd: float,
    adapter: str | None = None,
) -> str:
    """UMA chamada ao backend. Seam de teste: o fake troca esta função.

    Espelha o `add._call_author` (preflight, workspace descartável, teto de
    custo, resposta lida do trace pelo `add._result_text`) com o nome do backend
    parametrizado — o plano é mais caro que uma unit, mas continua sendo UMA
    chamada, e o texto da resposta é extraído pelo mesmo leitor de trace.

    `adapter` é o LoRA do planejador local (`--planner local`): planejar não é a
    mesma habilidade que executar, e o modelo base de 4B sem adapter de
    raciocínio devolve fila de um passo só.
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
                adapter=adapter,
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


def _parse_files(i: int, raw: object) -> tuple[str, ...]:
    """ "files" da sub-tarefa. Ausente = () — a spec só declara o que declarou."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DecomposeError(f"sub-tarefa {i}: files precisa ser lista de string")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise DecomposeError(f"sub-tarefa {i}: files tem item inválido: {item!r}")
        out.append(item.strip())
    if len(out) > MAX_FILES:
        raise DecomposeError(f"sub-tarefa {i}: {len(out)} files > teto {MAX_FILES}")
    return tuple(out)


def _parse_deps(i: int, raw: object, seen: list[str]) -> tuple[str, ...]:
    """ "deps" da sub-tarefa: só ids JÁ vistos no array.

    Dependência para a frente inverteria a ordem da fila — e a fila é executada
    em ordem, então ela travaria no primeiro passo esperando o último.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DecomposeError(f"sub-tarefa {i}: deps precisa ser lista de string")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise DecomposeError(f"sub-tarefa {i}: deps tem item inválido: {item!r}")
        dep = item.strip()
        if dep not in seen:
            raise DecomposeError(f"sub-tarefa {i}: dep {dep!r} não é sub-tarefa anterior do plano")
        out.append(dep)
    return tuple(out)


def _parse_checks(i: int, raw: object) -> tuple[dict[str, object], ...]:
    """ "checks" da sub-tarefa: régua graduada, nome/cmd/weight.

    Mesma régua de nome e de comando do `unit.toml` (`cli.CHECK_NAME_RE`,
    `add.validate_verify_cmd`): check torto reprovado aqui é erro de plano, não
    fila gravada que só quebra no `load_unit`.
    """
    if raw is None:
        return ()
    from harness.cli import CHECK_NAME_RE  # tardio: cli importa harness.add
    from harness.ruler.verify import VERIFY_CHECK_NAME

    if not isinstance(raw, list):
        raise DecomposeError(f"sub-tarefa {i}: checks precisa ser lista de objetos")
    if len(raw) > MAX_CHECKS:
        raise DecomposeError(f"sub-tarefa {i}: {len(raw)} checks > teto {MAX_CHECKS}")
    out: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DecomposeError(f"sub-tarefa {i}: check não é objeto JSON: {item!r}")
        missing = [k for k in ("name", "cmd", "weight") if k not in item]
        if missing:
            raise DecomposeError(f"sub-tarefa {i}: check sem campos: {', '.join(missing)}")
        name = str(item["name"]).strip()
        if not CHECK_NAME_RE.fullmatch(name) or name == VERIFY_CHECK_NAME:
            raise DecomposeError(f"sub-tarefa {i}: nome de check inválido: {name!r}")
        if any(c["name"] == name for c in out):
            raise DecomposeError(f"sub-tarefa {i}: check duplicado: {name}")
        cmd = str(item["cmd"]).strip()
        try:
            add.validate_verify_cmd(cmd)
        except AddError as exc:
            raise DecomposeError(f"sub-tarefa {i}: check {name}: {exc}") from None
        try:
            weight = float(item["weight"])
        except (TypeError, ValueError):
            raise DecomposeError(f"sub-tarefa {i}: check {name}: weight não é número") from None
        if not weight > 0 or weight == float("inf"):
            raise DecomposeError(f"sub-tarefa {i}: check {name}: weight precisa ser > 0")
        out.append({"name": name, "cmd": cmd, "weight": weight})
    return tuple(out)


def parse_plan(text: str, n_max: int = DEFAULT_N_MAX) -> list[dict[str, object]]:
    """Array JSON → specs validadas pelo `add`. Qualquer desvio é DecomposeError.

    Cada item volta pelo `add.parse_author_json` (via re-dump): é ele que julga
    slug, prompt vazio, kind e `validate_verify_cmd`. Duplicar aqui a régua de
    verify determinístico seria criar a segunda régua que ninguém sincroniza.
    `files`/`deps`/`checks` são campos DESTE módulo (o `add` não os conhece), e
    por isso são validados aqui — com o mesmo tratamento de erro dos demais.
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

    specs: list[dict[str, object]] = []
    seen: list[str] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise DecomposeError(f"sub-tarefa {i} não é objeto JSON: {item!r}")
        try:
            spec: dict[str, object] = dict(
                add.parse_author_json(json.dumps(item, ensure_ascii=False))
            )
        except AddError as exc:
            raise DecomposeError(f"sub-tarefa {i} inválida: {exc}") from None
        spec["files"] = _parse_files(i, item.get("files"))
        spec["deps"] = _parse_deps(i, item.get("deps"), seen)
        spec["checks"] = _parse_checks(i, item.get("checks"))
        specs.append(spec)
        seen.append(str(spec["id_slug"]))
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


def planner_backend(planner: str) -> tuple[str, str | None]:
    """`(backend, adapter)` do planejador escolhido. `remote` é o default de sempre."""
    if planner == "local":
        return LOCAL_PLANNER_BACKEND, LOCAL_PLANNER_ADAPTER
    if planner != "remote":
        raise DecomposeError(f"planner desconhecido: {planner!r} (use {', '.join(PLANNERS)})")
    return DECOMPOSE_BACKEND, None


def default_model(backend: str) -> str | None:
    """Modelo default do planejador. `DECOMPOSE_MODEL` é um id do `claude_code`
    (`haiku`) — mandá-lo para a frota local seria pedir um modelo que não existe
    lá. Backend que não é o de origem escolhe o próprio modelo (None)."""
    return DECOMPOSE_MODEL if backend == DECOMPOSE_BACKEND else None


def gate_prompt(base: str, problems: list[str]) -> str:
    """O prompt de novo, com o veredito do gate anexado.

    Reprovação sem o motivo devolvido ao planejador é a mesma chamada outra vez
    com outra semente — e o modelo repete o erro. O que o gate mediu (inclusive
    o verify que já passava no HEAD) é a única informação nova que existe.
    """
    return (
        f"{base}\n\n"
        "ATENÇÃO — seu plano anterior foi REPROVADO pelo gate. Motivos, um por linha:\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\n\nRefaça o plano inteiro corrigindo TODOS os motivos acima. Em especial: "
        "todo `verify_cmd` tem que FALHAR no repositório como ele está agora (se ele já "
        "passa, o passo não tem trabalho nenhum ou a régua mede a coisa errada), e todo "
        "passo precisa de `checks`. Responda de novo APENAS com o array JSON."
    )


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
    adapter: str | None = None,
    err=None,
) -> DecomposeProposal | None:
    """Plano de sub-units APROVADO PELO GATE, ou None.

    None é o contrato do `pick_target`/`propose_redteam`: saída de backend que
    não é um plano válido — ou que "decompôs" em uma única unit — não vira fila
    pela metade. Problema de SETUP (projeto não registrado, backend indisponível,
    teto de custo) levanta DecomposeError: aí não falta gradiente, falta config,
    e engolir isso em None esconderia a causa de quem chamou.

    O gate (`plan_gate.check_plan`) roda ANTES do retorno, e a reprovação vale
    UMA re-chamada com os motivos anexados. Duas reprovações é o sinal de que o
    planejador não está entendendo a tarefa — insistir com o mesmo modelo é
    queimar orçamento, e o stderr diz para escalar.
    """
    task = task.strip()
    if not task:
        raise DecomposeError("tarefa vazia")
    if n_max < MIN_UNITS:
        raise DecomposeError(f"n_max={n_max} < mínimo {MIN_UNITS}")
    err = err if err is not None else sys.stderr
    repo = add.load_project_repo(project, projects_file)
    queue = queue_dir_for(project, queue_dir, projects_path)
    context = add.project_context(repo)
    base = planning_prompt(task, project, context, n_max)
    first = next_index(queue) if start is None else start

    from harness.improve import plan_gate

    prompt = base
    for attempt in (1, 2):
        text = _call_planner(prompt, backend, model or default_model(backend), max_usd, adapter)
        try:
            specs = parse_plan(text, n_max)
        except DecomposeError:
            return None
        units = tuple(
            SubUnit(index=first + i, name=f"{first + i:02d}_{spec['id_slug']}", spec=spec)
            for i, spec in enumerate(specs)
        )
        proposal = DecomposeProposal(
            task=task, project=project, units=units, backend=backend, queue=queue
        )
        problems = plan_gate.check_plan(proposal, repo)
        for warn in (p for p in problems if p.startswith(plan_gate.WARN)):
            print(f"decompose: {warn}", file=err)
        bad = plan_gate.errors(problems)
        if not bad:
            return proposal
        for problem in bad:
            print(f"decompose: gate reprovou — {problem}", file=err)
        if attempt == 2:
            print(
                f"decompose: plano reprovado no gate duas vezes para {project} — "
                "nada gravado. O planejador não está entendendo a tarefa: escale para "
                "revisão humana (ou um modelo maior em --model) antes de tentar de novo.",
                file=err,
            )
            return None
        prompt = gate_prompt(base, bad)
    return None  # pragma: no cover - o laço sempre retorna


def _dep_name(slug: str, proposal: DecomposeProposal) -> str:
    """slug do plano -> nome/id da unit na fila (`01_slug`).

    O plano fala em `id_slug`; quem executa a fila e quem lê o ledger falam no
    `id` da unit, que é o nome do diretório. Emitir o slug cru em `deps` daria
    uma dependência que não bate com unidade nenhuma.
    """
    for other in proposal.units:
        if other.slug == slug:
            return other.name
    return slug


def render_unit_toml(unit: SubUnit, proposal: DecomposeProposal) -> str:
    """Formato das units de projeto (com `project`) + `[origin]` da procedência.

    `add._s` em vez de um serializador local pelo motivo do `redteam`: duas
    rotinas string->TOML é como os dois formatos divergem sem ninguém notar.
    """
    total = len(proposal.units)
    deps = tuple(str(d) for d in unit.spec.get("deps") or ())
    files = tuple(str(f) for f in unit.spec.get("files") or ())
    checks = tuple(unit.spec.get("checks") or ())
    lines = [
        "# Autorado por `harness decompose` — passo "
        f"{unit.index - proposal.units[0].index + 1}/{total} de uma fila progressiva.",
        "# A ordem alfabética da fila é a dependência: este passo assume o"
        " estado que o anterior deixou.",
        f"id = {add._s(unit.name)}",
        f"kind = {add._s(unit.spec['kind'])}",
        f"project = {add._s(proposal.project)}",
        f"prompt = {add._s(unit.spec['prompt_md'])}",
        f"verify_cmd = {add._s(unit.spec['verify_cmd'])}",
        # `deps` carrega o NOME da unit anterior na fila (`01_slug`), não o slug
        # cru: é ele que vira o `id` da unit e fecha o join no ledger.
        f"deps = [{', '.join(add._s(_dep_name(d, proposal)) for d in deps)}]",
        f"files = [{', '.join(add._s(f) for f in files)}]",
        "",
    ]
    # `[checks.<name>]` no formato que o `cli._load_checks` lê. Antes do
    # `[origin]` porque em TOML tudo que vem depois de uma tabela pertence a ela.
    for check in checks:
        assert isinstance(check, dict)
        lines += [
            f"[checks.{check['name']}]",
            f"cmd = {add._s(str(check['cmd']))}",
            f"weight = {float(check['weight'])}",
            "",
        ]
    return "\n".join(
        [
            *lines,
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
