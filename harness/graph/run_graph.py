"""run_graph: a topologia de um run.

    plan → route → provision → execute → verify → measure → gate
                                                     ↓
              accept ──┐   retry → route   escalate ──┐   revert ──┐
                       └──────────── record ──────────┴────────────┘ → END

`measure` coleta KPI (ruler/kpi) com specs lidas ANTES do execute; `gate` aplica
a régua real (ruler/gate) com prova de tamper (genome/tamper) tirada no
provision. `config/graph.toml` calibra o comportamento (teto de tentativas,
timeout do verify, toggles por nó) — a topologia é código imutável no genoma, a
política é config mutável. A espinha do arquivo segue a idempotência: toda
escrita externa passa por `(run_id, node, attempt)` no ledger, então matar o
processo no meio de `execute` e reinvocar o mesmo `thread_id` retoma sem
reexecutar nada que já aconteceu.

O `attempt` faz parte da chave só dos nós por-tentativa (`execute`, `verify`):
sem ele o braço `retry` seria decorativo — a segunda passagem acharia o registro
da primeira e devolveria o resultado cacheado, e nem `max_attempts` nem a
escalação de tier do router mudariam o desfecho. Os nós que rodam uma vez por
run (`plan`, `provision`, `record`) ficam no attempt 0.

`route` tem dois modos (`harness_route`): `manual` honra o backend que o
chamador fixou — é o default, e o que a CLI faz hoje — e `auto` entrega a
escolha ao `routing.router`. `route` de propósito NÃO é idempotente por
attempt: é o único nó que precisa recalcular a cada passagem, senão o braço
`retry` voltaria a rodar no mesmo tier que acabou de falhar.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.backends import procs, registry
from harness.genome import tamper
from harness.genome.genome import DEFAULT_PATH as GENOME_PATH
from harness.genome.genome import Genome
from harness.genome.genome import load as load_genome
from harness.governor.governor import (
    CUTOFF,
    check_cost,
    check_deadline,
    load_gov,
    taper_turns,
)
from harness.graph.checkpoint import open_checkpointer
from harness.graph.state import Budget, Decision, Event, RunState
from harness.ledger import store
from harness.projects import (
    deliver,
    discard_run_branch,
    get_project,
    run_branch,
)
from harness.routing import (
    MANUAL_TIER,
    ROUTE_AUTO,
    ROUTE_MANUAL,
    ROUTE_MODES,
    config_dir,
    router,
)
from harness.ruler.gate import gate as ruler_gate
from harness.ruler.kpi import KpiSpec, collect, load_kpis
from harness.ruler.verify import (
    CHECKS_EXIT,
    graded_score,
    log_tail,
    run_extra_checks,
    run_log_dir,
)
from harness.types import ExecRequest, ExecResult, RunRow, Selection, UnitSpec, Verdict
from harness.workspace import setup as ws_setup
from harness.workspace.provision import add_worktree, remove_worktree
from harness.workspace.sealing import VERIFIER_NAMES, is_verifier, verifier_visible

# Chaves nossas dentro de `config["configurable"]`. O que não é estado do run
# (para onde escrever, quem executa) viaja por aqui, não pelo checkpoint.
CFG_DATA_DIR = "harness_data_dir"
CFG_BACKEND = "harness_backend"
CFG_MODEL = "harness_model"
CFG_MAX_TURNS = "harness_max_turns"
CFG_ROUTE = "harness_route"

DEFAULT_MAX_TURNS = 30  # auditoria 2026-08-03: 8 model calls não cobrem ler+editar+verificar
VERIFY_TIMEOUT_S = 120.0
VERIFY_LOG = "verify.log"
TRACE_FILE = "trace.jsonl"

# Não vão para o workspace: a spec da unidade e lixo de ferramenta.
IGNORE_NAMES = ("unit.toml", "__pycache__", ".git", ".venv", "node_modules")

GRAPH_TOML = "graph.toml"
DEFAULT_MAX_ATTEMPTS = 2

# ui-verify dentro do grafo: build verde não é prova de tela viva (um dist cujo
# único stylesheet aponta pra caminho morto sai 0 no build e chega cru no
# navegador). Opt-in por `nodes.ui_verify`, porque só unidade com resultado
# visual tem dist para olhar.
UI_VERIFY_DIST = "dist"
UI_VERIFY_EXPECT = ("css",)
UI_VERIFY_EXIT = 65   # veredito derrubado pela TELA, não pelo `verify_cmd`

# Gate de delta: quantas tentativas sem ganho de score toleram antes de chamar
# gente. Uma é ruído do executor (mesma nota por caminho diferente); duas é
# platô — a terceira tentativa custaria um turno pelo mesmo lugar.
DELTA_STAGNATIONS = 2
DELTA_REASON = "sem_gradiente_de_score"


# --- política calibrável ------------------------------------------------------


@dataclass(frozen=True)
class GraphPolicy:
    """O que o loop pode calibrar sem tocar a topologia. Vive em
    `config/graph.toml` (genoma-mutável); os defaults daqui valem quando o
    arquivo não existe ou está torto."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    verify_timeout_s: float = VERIFY_TIMEOUT_S
    measure: bool = True
    tamper: bool = True
    ui_verify: bool = False
    ui_verify_dist: str = UI_VERIFY_DIST
    # delta_gate: retry só continua enquanto a régua graduada MEXE. Ligado por
    # default — o objetivo é economizar turno; `false` volta ao retry cego.
    delta_gate: bool = True


def _policy_int(raw: Any, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def _policy_float(raw: Any, default: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _policy_bool(raw: Any, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _policy_str(raw: Any, default: str) -> str:
    return raw if isinstance(raw, str) and raw.strip() else default


def load_policy(path: Path | None = None) -> GraphPolicy:
    """`config/graph.toml` -> GraphPolicy. Falha aberta campo a campo: arquivo
    ausente/ilegível ou campo torto cai no default — config ruim degrada para o
    comportamento de fábrica, nunca derruba o run."""
    p = Path(path) if path is not None else config_dir() / GRAPH_TOML
    base = GraphPolicy()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return base
    nodes = data.get("nodes")
    nodes = nodes if isinstance(nodes, dict) else {}
    return GraphPolicy(
        max_attempts=_policy_int(data.get("max_attempts"), base.max_attempts),
        verify_timeout_s=_policy_float(
            data.get("verify_timeout_s"), base.verify_timeout_s
        ),
        measure=_policy_bool(nodes.get("measure"), base.measure),
        tamper=_policy_bool(nodes.get("tamper"), base.tamper),
        ui_verify=_policy_bool(nodes.get("ui_verify"), base.ui_verify),
        ui_verify_dist=_policy_str(data.get("ui_verify_dist"), base.ui_verify_dist),
        delta_gate=_policy_bool(nodes.get("delta_gate"), base.delta_gate),
    )


# --- helpers de config/serialização ------------------------------------------


def _gov_deadline(run_id: str, db: Path, gov=None) -> str:
    """'continue'/'cutoff' contra o `started_ts` que o plan gravou no ledger
    (wall clock: vale mesmo com resume noutro processo). Fail-open: sem plan
    ou ts torto -> 'continue' — comportamento atual intacto."""
    if gov is None:
        gov = load_gov()
    plan_ev = store.get_node(run_id, "plan", db) or {}
    try:
        started = float(plan_ev["started_ts"])
    except (KeyError, TypeError, ValueError):
        return "continue"
    return check_deadline(started, time.time(), gov)


def _gov_cost(run_id: str, db: Path, attempt: int, gov=None) -> str:
    """'continue'/'cutoff' contra o gasto ACUMULADO das tentativas deste run.

    A soma sai do ledger (payload de cada `execute`), não do estado: o resume
    noutro processo tem que herdar a conta da tentativa que já foi paga — teto
    de gasto que zera a cada processo não é teto. Custo ausente ou torto conta
    zero, e a checagem só corta com `cost_cap_usd > 0`: fail-open igual ao prazo.
    """
    if gov is None:
        gov = load_gov()
    spent = 0.0
    for a in range(max(0, attempt) + 1):
        ev = store.get_node(run_id, "execute", db, attempt=a) or {}
        try:
            spent += float(ev.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    return check_cost(spent, gov)


def _score_series(run_id: str, db: Path, attempt: int) -> list[float]:
    """Scores graduados das tentativas 0..attempt, na ordem, do ledger.

    Do LEDGER e não do estado pelo mesmo motivo do `_gov_cost`: a nota da
    tentativa anterior tem que sobreviver ao resume noutro processo — o estado
    só carrega o veredito corrente (`_retry` limpa o resto de propósito).
    Payload sem `score` (gravado antes da régua graduada existir) não entra:
    fingir 1.0 aqui inventaria um gradiente que ninguém mediu.
    """
    out: list[float] = []
    for a in range(max(0, int(attempt)) + 1):
        ev = store.get_node(run_id, "verify", db, attempt=a) or {}
        if "score" not in ev:
            continue
        try:
            out.append(float(ev["score"]))
        except (TypeError, ValueError):
            continue
    return out


def _stagnations(scores: Sequence[float]) -> int:
    """Quantas tentativas seguidas não subiram a nota (`<=` conta como parada).

    Empate é estagnação de propósito: mesma nota por caminho diferente é a
    assinatura de quem está batendo na mesma parede.
    """
    return sum(1 for prev, now in zip(scores, scores[1:]) if now <= prev)


def _cfg(config, key: str, default: Any = None) -> Any:
    return ((config or {}).get("configurable") or {}).get(key, default)


def _db(config) -> Path:
    return Path(_cfg(config, CFG_DATA_DIR, "data")) / store.DB_NAME


def _event(node: str, **extra: Any) -> Event:
    return {"node": node, "at": store.now_iso(), **extra}


def _exec_payload(res: ExecResult) -> dict:
    return {
        "ok": res.ok,
        "exit_reason": res.exit_reason,
        "turns": res.turns,
        "cost_usd": res.cost_usd,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "files_changed": list(res.files_changed),
        "session_id": res.session_id,
        "trace_path": str(res.trace_path),
    }


def _exec_from_payload(d: dict) -> ExecResult:
    return ExecResult(
        ok=bool(d["ok"]),
        exit_reason=d["exit_reason"],
        turns=int(d["turns"]),
        cost_usd=d["cost_usd"],
        tokens_in=d["tokens_in"],
        tokens_out=d["tokens_out"],
        files_changed=tuple(d["files_changed"]),
        session_id=d["session_id"],
        trace_path=Path(d["trace_path"]),
    )


def _verdict_payload(v: Verdict) -> dict:
    return {
        "passed": v.passed,
        "exit_code": v.exit_code,
        "log_path": str(v.log_path),
        "sec": v.sec,
        "score": v.score,
        "failed": list(v.failed),
    }


def _verdict_from_payload(d: dict) -> Verdict:
    # `score`/`failed` ausentes: payload gravado antes da régua graduada existir.
    # Retomar um run velho não pode virar KeyError.
    return Verdict(
        passed=bool(d["passed"]),
        exit_code=int(d["exit_code"]),
        log_path=Path(d["log_path"]),
        sec=float(d["sec"]),
        score=float(d.get("score", 1.0)),
        failed=tuple(d.get("failed") or ()),
    )


def _episode_trace(v: Verdict, tail: str) -> str:
    """Trace do episódio com os checks reprovados na frente.

    Só os NOMES: o episódio é lido por um modelo na próxima vez que o mesmo kind
    aparecer, e nome de check é contrato público do `unit.toml` — o texto do log
    do verificador selado continua sendo o único conteúdo, e continua sem ir para
    prompt nenhum (`reflect.build_hint`).
    """
    if not v.failed:
        return tail
    return f"checks_falhos: {','.join(v.failed)}\n{tail}"


def _record_episode(unit: UnitSpec, trace: str) -> None:
    """Ponta de escrita da memória episódica: verify vermelho vira caso passado.

    Episódio é conhecimento global entre runs/experimentos: grava sempre no
    ledger default, nunca no `_db(config)` do run — a leitura (`_episodic_block`)
    também é global, e escrever no db do experimento tornaria o episódio
    invisível.

    Mesmo fail-open do `_episodic_block` que consome isto no backend — import
    lazy (o módulo pode não existir num genoma antigo) e except largo, porque
    memória que derruba o run vale menos que memória nenhuma."""
    try:
        from harness.memory import episodic

        episodic.record_failure(unit.kind, unit.id, trace)
    except Exception:
        pass


def _specs_payload(specs: Mapping[str, KpiSpec]) -> dict:
    return {
        n: {"cmd": s.cmd, "direction": s.direction, "timeout_s": s.timeout_s}
        for n, s in specs.items()
    }


def _specs_from_payload(d: dict | None) -> dict[str, KpiSpec]:
    return {
        n: KpiSpec(
            name=n,
            cmd=v["cmd"],
            direction=v.get("direction", "higher"),
            timeout_s=v.get("timeout_s"),
        )
        for n, v in (d or {}).items()
    }


def _baseline(ws: Path, policy: GraphPolicy) -> dict:
    """O ANTES da mudança, medido pré-execute e congelado no payload do
    provision: specs+valores de KPI e o fingerprint do imutável. É o que impede
    a mudança avaliada de redefinir a própria régua (Goodhart do kpis.toml) ou
    de mexer no imutável sem ninguém notar."""
    out: dict = {"kpi_specs": {}, "kpi_before": {}, "tamper_fp": None, "genome": None}
    if policy.measure:
        specs = load_kpis(ws)  # sem kpis.toml => {} — KPI é opcional
        out["kpi_specs"] = _specs_payload(specs)
        out["kpi_before"] = collect(ws, specs=specs) if specs else {}
    if policy.tamper:
        gpath = ws / GENOME_PATH
        if gpath.is_file():
            try:
                g = load_genome(gpath)
            except ValueError:
                g = None  # genoma torto no alvo: sem régua de tamper, não crash
            if g is not None:
                out["genome"] = {
                    "immutable": list(g.immutable),
                    "mutable": list(g.mutable),
                }
                out["tamper_fp"] = tamper.fingerprint(g, ws)
    return out


def _copy_unit_files(src: Path, dst: Path) -> None:
    """Materializa o dir da unidade no workspace. Sobrescreve: é idempotente.

    O verificador não vem: `_verify` o materializa depois do execute.
    """
    ignore = shutil.ignore_patterns(*IGNORE_NAMES, *VERIFIER_NAMES)
    for item in sorted(src.iterdir()):
        if item.name in IGNORE_NAMES or is_verifier(item.name):
            continue
        if item.is_dir():
            shutil.copytree(item, dst / item.name, dirs_exist_ok=True, ignore=ignore)
        else:
            shutil.copy2(item, dst / item.name)


# --- nós ----------------------------------------------------------------------
#
# `config` fica sem anotação de propósito: o langgraph só injeta o config quando
# a anotação é `RunnableConfig` ou quando não existe anotação nenhuma. Sem
# anotação o núcleo não precisa nomear um tipo de vendor.


# Gate do plano: barato de propósito — regex e tamanho, zero chamada de LLM. O
# nó `plan` roda FORA do executor (não tem agente para perguntar), então ele só
# carimba o flag e o `_prompt` transforma isso em uma ordem no prompt.
PLAN_PROMPT_CHARS = 400
PLAN_TRIGGERS = re.compile(
    r"\b(refator\w*|refactor\w*|implement\w*|reescrev\w*|migr\w*)\b|"
    r"\b(cada|todos|todas)\s+(os|as)\s+arquivos?\b|"
    r"\barquivos\b",
    re.IGNORECASE,
)
PLAN_ORDER = (
    "ANTES DE TUDO: chame task(subagent_type='planner') e transcreva o plano "
    "com write_todos."
)


def _needs_plan(prompt: str) -> bool:
    """Tarefa grande o bastante para valer um plano antes da primeira edição."""
    return len(prompt) > PLAN_PROMPT_CHARS or PLAN_TRIGGERS.search(prompt) is not None


def _plan(state: RunState, config=None) -> dict:
    """Valida a unidade e fixa a identidade do run."""
    unit = state["unit"]
    if not isinstance(unit, UnitSpec):
        raise TypeError("state['unit'] precisa ser UnitSpec")
    missing = [f for f in ("id", "prompt", "verify_cmd") if not getattr(unit, f)]
    if missing:
        raise ValueError(f"unit inválida: campos vazios: {', '.join(missing)}")

    run_id = state.get("run_id") or uuid.uuid4().hex[:12]
    # `started_ts` é wall clock de propósito: o resume acontece noutro processo.
    needs_plan = _needs_plan(unit.prompt)
    store.record_node(run_id, "plan", {"started_ts": time.time()}, _db(config))
    return {
        "run_id": run_id,
        "attempt": state.get("attempt", 0),
        "needs_plan": needs_plan,
        "events": [_event("plan", run_id=run_id, unit=unit.id, needs_plan=needs_plan)],
    }


def _route(state: RunState, config=None) -> dict:
    """Quem paga esta tentativa. `auto` pergunta ao router, `manual` obedece.

    No modo `auto` o histórico do ledger vai inteiro para o `select`: o filtro
    que importa (kind, tier, backend) é do router, e recortar aqui por fora
    esconderia amostra dele. O `attempt` corrente é o que faz a segunda passagem
    subir de tier — a escalação é do router, não deste nó.
    """
    unit = state["unit"]
    attempt = state.get("attempt", 0)

    if _cfg(config, CFG_ROUTE, ROUTE_MANUAL) == ROUTE_AUTO:
        selection = router.select(
            unit,
            history=store.history(path=_db(config)),
            attempt=attempt,
        )
    else:
        selection = Selection(
            backend=_cfg(config, CFG_BACKEND, "mock"),
            model=_cfg(config, CFG_MODEL) or "",
            tier=MANUAL_TIER,
            kind=unit.kind or "code",
            max_turns=int(_cfg(config, CFG_MAX_TURNS, DEFAULT_MAX_TURNS)),
            reasons=("manual:pedido_do_chamador",),
        )

    # Governor: o chefe aperta a cada passagem (route é o único nó que roda de
    # novo a cada tentativa). Taper no max_turns; o prazo é consultado aqui e
    # carimbado no trace, mas o desvio para escalate acontece no gate — único
    # ponto condicional da topologia. Sem governor.toml, tudo é no-op.
    gov = load_gov()
    turns = taper_turns(selection.max_turns, attempt, gov)
    if turns != selection.max_turns:
        selection = replace(selection, max_turns=turns)
    deadline = _gov_deadline(state["run_id"], _db(config), gov)

    return {
        "selection": selection,
        "events": [
            _event(
                "route",
                deadline=deadline,
                backend=selection.backend,
                model=selection.model,
                tier=selection.tier,
                kind=selection.kind,
                attempt=attempt,
                # `reasons` no trace: sem ela a escolha vira número sem porquê.
                reasons=list(selection.reasons),
            )
        ],
    }


def _provision(state: RunState, config=None) -> dict:
    """Workspace por run. Se já existe, reusa; a cópia só acontece uma vez."""
    run_id = state["run_id"]
    db = _db(config)
    ws = Path(_cfg(config, CFG_DATA_DIR, "data")) / "ws" / run_id
    ws.mkdir(parents=True, exist_ok=True)

    saved = store.get_node(run_id, "provision", db)
    if saved is not None:
        return {
            "workspace": saved["workspace"],
            "events": [_event("provision", workspace=saved["workspace"], reused=True)],
        }

    t0 = time.monotonic()
    unit = state["unit"]
    if unit.project:
        # Projeto real (opt-in por `project=` na unidade): o workspace é um git
        # worktree do repo registrado, em branch efêmera a partir do HEAD. A
        # working tree principal do repo nunca é tocada — isolamento por
        # construção. Sem `project` o caminho default abaixo continua igual.
        ws = ws.resolve()  # `git -C repo worktree add` exige path absoluto
        add_worktree(get_project(unit.project).repo, ws, branch=run_branch(run_id))
    _copy_unit_files(unit.path, ws)
    setup = {"skipped": True, "sec": 0.0, "ok": True}
    if unit.project:
        # Fail-open: setup que quebra segue para o executor (que pode consertar
        # o ambiente). O evento carrega `setup_failed` para o trace não perder.
        setup = ws_setup.ensure(ws, get_project(unit.project))
    sec = time.monotonic() - t0
    # Baseline junto do workspace: um SIGKILL entre copiar e medir não pode
    # deixar um provision "pela metade" — payload único, escrita única.
    payload = {
        "workspace": str(ws),
        "sec": sec,
        "sec_setup": setup["sec"],
        "setup_skipped": setup["skipped"],
        "setup_failed": not setup["ok"],
        **_baseline(ws, load_policy()),
    }
    store.record_node(run_id, "provision", payload, db)
    return {
        "workspace": str(ws),
        "events": [
            _event(
                "provision",
                workspace=str(ws),
                reused=False,
                sec_setup=setup["sec"],
                setup_skipped=setup["skipped"],
                setup_failed=not setup["ok"],
            )
        ],
    }


def _prompt(state: RunState) -> str:
    """Prompt da tentativa: o da unidade, mais o hint do checker se houver.

    Import lazy do reflect para não amarrar run_graph a um módulo que a
    topologia pode nem usar; sem hint (default) a string é a de sempre.

    A ordem do plano vai PRIMEIRO (`needs_plan` carimbado no nó `plan`): o
    executor pequeno obedece a primeira linha do prompt, não a última.
    """
    base = state["unit"].prompt
    if state.get("needs_plan"):
        base = f"{PLAN_ORDER}\n\n{base}"
    hint = str(state.get("reflect_hint") or "").strip()
    if not hint:
        return base

    from harness.graph.reflect import HINT_HEADER
    from harness.improve.escalate import prior_decisions

    out = f"{base}\n\n{HINT_HEADER}\n{hint}"
    # Mesmo bloco que a escalação mostra ao humano, do lado de quem vai tentar
    # de novo: se um humano já respondeu uma parada deste kind com este motivo,
    # a tentativa seguinte decide sabendo do precedente. "" quando não há.
    prior = prior_decisions(state["unit"].kind, hint)
    return f"{out}\n\n{prior}" if prior else out


def _execute(state: RunState, config=None) -> dict:
    """Chama o backend — uma vez por tentativa, custe o que custar o crash."""
    run_id = state["run_id"]
    attempt = state.get("attempt", 0)
    db = _db(config)
    saved = store.get_node(run_id, "execute", db, attempt=attempt)
    if saved is not None:
        return {
            "exec": _exec_from_payload(saved),
            "events": [_event("execute", reused=True, attempt=attempt)],
        }

    sel = state["selection"]
    ws = Path(state["workspace"])
    result = registry.get_backend(sel.backend).execute(
        ExecRequest(
            prompt=_prompt(state),
            workspace=ws,
            model=sel.model or None,
            max_turns=sel.max_turns,
            trace_path=ws / TRACE_FILE,
            run_id=run_id,
            kind=sel.kind,
        )
    )
    payload = _exec_payload(result)
    payload["trace_saved"] = _save_trace(
        result.trace_path, run_id, attempt, _cfg(config, CFG_DATA_DIR, "data")
    )
    store.record_node(run_id, "execute", payload, db, attempt=attempt)
    return {
        "exec": result,
        "events": [
            _event(
                "execute",
                ok=result.ok,
                exit_reason=result.exit_reason,
                attempt=attempt,
            )
        ],
    }


def _save_trace(trace: Path, run_id: str, attempt: int, data_dir: Any) -> bool:
    """Copia o trace da tentativa para fora do workspace, antes do dispose.

    O trace é a única evidência do que o agente FEZ (turno a turno), e vive num
    tmpdir que a próxima tentativa sobrescreve e o teardown apaga: sem esta cópia
    todo post-mortem depende de o run ter falhado no lugar certo. Um arquivo por
    tentativa, ao lado do log do verify.

    Fail-open: trace inexistente ou disco recusando não pode derrubar um execute
    que já foi pago — só marca `trace_saved: false` no payload.
    """
    try:
        dst = run_log_dir(run_id, data_dir) / f"trace.{attempt}.jsonl"
        shutil.copyfile(trace, dst)
        return True
    except OSError:
        return False


def _ui_verify(ws: Path, policy: GraphPolicy) -> list[str]:
    """A régua olha a TELA: serve o `dist/` do workspace, exige que todo asset
    local carregue e que o screenshot tenha tamanho de página com conteúdo.

    Fail-open em tudo que não é a tela — dist ausente, navegador ausente, módulo
    indisponível — porque aqui a falha derruba um veredito de run inteiro: em
    máquina sem Chrome isso viraria DISCARD sem evidência nenhuma. Quem quer o
    rigor de "não verificado é reprovado" põe `harness ui-verify` no próprio
    `verify_cmd` da unidade, onde Chrome ausente É falha.

    Sem `strict_links` de propósito: numa fila progressiva o nav da primeira
    unidade linka páginas que a última ainda vai criar, e reprovar por isso
    derrubava run cuja tela renderizou perfeitamente. Quem quer o gate de
    completude põe `harness ui-verify --strict-links` no `verify_cmd`.
    """
    dist = ws / policy.ui_verify_dist
    if not dist.is_dir():
        return []
    try:
        from harness import uiverify

        res = uiverify.verify(
            dist, expect=UI_VERIFY_EXPECT, shot_out=ws / uiverify.SHOT_NAME
        )
    except Exception:
        return []
    if uiverify.MISSING_CHROME in res.failures:
        return []
    return list(res.failures)


def _verify(state: RunState, config=None) -> dict:
    """A régua roda o `verify_cmd` da unidade. PR-4 troca isto por `ruler/verify`."""
    run_id = state["run_id"]
    attempt = state.get("attempt", 0)
    db = _db(config)
    saved = store.get_node(run_id, "verify", db, attempt=attempt)
    if saved is not None:
        return {
            "verdict": _verdict_from_payload(saved),
            "events": [_event("verify", reused=True, attempt=attempt)],
        }

    ws = Path(state["workspace"])
    # Fora do ws, um arquivo por tentativa: no ws o verificador selado imprimiria
    # o golden e a tentativa seguinte o leria como resposta.
    log_path = (
        run_log_dir(run_id, _cfg(config, CFG_DATA_DIR, "data")) / f"verify.a{attempt}.log"
    )
    t0 = time.monotonic()
    # Prova selada: o verificador só existe no workspace dentro deste `with` —
    # o agente já rodou e a tentativa seguinte também não vai vê-lo.
    with verifier_visible(state["unit"].path, ws):
        try:
            proc = subprocess.run(
                state["unit"].verify_cmd,
                shell=True,
                cwd=ws,
                capture_output=True,
                timeout=load_policy().verify_timeout_s,
            )
            exit_code, out = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            exit_code, out = 124, b"verify: timeout\n"

    # `verify_cmd` verde ainda não é tela viva: com `nodes.ui_verify` a tela
    # entra no MESMO veredito, e o motivo entra no MESMO log (é ele que vira o
    # tail do ledger). Só sobre comando que passou — reprovado já reprovou.
    policy = load_policy()
    ui_fail = _ui_verify(ws, policy) if policy.ui_verify and exit_code == 0 else []
    if ui_fail:
        out += "".join(f"ui-verify FALHA {m}\n" for m in ui_fail).encode()
        exit_code = UI_VERIFY_EXIT

    # Régua graduada (`[checks]` da unidade): mesmo padrão do ui_fail — as linhas
    # entram no MESMO log e o veredito é UM. Rodam sempre (inclusive com o
    # comando principal vermelho: é aí que "quanto passou" informa o retry), mas
    # só derrubam exit code que estava verde. Orçamento próprio, como o do
    # comando principal.
    checks = state["unit"].checks
    extra_score, failed, checks_log = run_extra_checks(
        checks, ws, budget_s=policy.verify_timeout_s
    )
    if checks_log:
        out += checks_log.encode()
    verify_ok = exit_code == 0
    if failed and verify_ok:
        exit_code = CHECKS_EXIT
    log_path.write_bytes(out)

    verdict = Verdict(
        passed=exit_code == 0,
        exit_code=exit_code,
        log_path=log_path,
        sec=time.monotonic() - t0,
        score=graded_score(verify_ok, checks, extra_score),
        failed=failed,
    )
    # Sem isto o ledger guarda só `exit=N`: o motivo real fica no workspace, que
    # o retry sobrescreve e o tmpdir descarta.
    tail = "" if verdict.passed else log_tail(log_path)
    payload = _verdict_payload(verdict)
    if tail:
        payload["tail"] = tail
        # Veredito fechado e vermelho: daqui o gate só sai por retry/revert, então
        # este é o caso a lembrar no próximo run do mesmo kind.
        _record_episode(state["unit"], _episode_trace(verdict, tail))
    store.record_node(run_id, "verify", payload, db, attempt=attempt)
    # `score`/`failed` no evento só quando há régua graduada: sem `[checks]` o
    # evento é o de sempre, chave por chave.
    graded = {"score": verdict.score, "failed": list(failed)} if checks else {}
    return {
        "verdict": verdict,
        "events": [
            _event(
                "verify",
                passed=verdict.passed,
                exit_code=exit_code,
                attempt=attempt,
                **({"tail": tail} if tail else {}),
                **graded,
            )
        ],
    }


def _measure(state: RunState, config=None) -> dict:
    """KPIs do DEPOIS, medidos com as specs do ANTES (payload do provision).

    Por tentativa, como execute/verify: o retry muda o workspace, então o
    "depois" da tentativa 1 não é o da tentativa 0. `nodes.measure = false`
    devolve o stub antigo: campos vazios, gate cego a KPI, nada no ledger.
    """
    run_id = state["run_id"]
    attempt = state.get("attempt", 0)
    db = _db(config)
    saved = store.get_node(run_id, "measure", db, attempt=attempt)
    if saved is not None:
        return {
            "kpi_before": saved["before"],
            "kpi_after": saved["after"],
            "events": [_event("measure", reused=True, attempt=attempt)],
        }
    if not load_policy().measure:
        return {"kpi_before": {}, "kpi_after": {}, "events": [_event("measure")]}

    prov = store.get_node(run_id, "provision", db) or {}
    specs = _specs_from_payload(prov.get("kpi_specs"))
    before = prov.get("kpi_before") or {}
    after = collect(Path(state["workspace"]), specs=specs) if specs else {}
    store.record_node(
        run_id, "measure", {"before": before, "after": after}, db, attempt=attempt
    )
    return {
        "kpi_before": before,
        "kpi_after": after,
        "events": [_event("measure", attempt=attempt, kpis=sorted(specs))],
    }


def _gate(state: RunState, config=None) -> dict:
    """A régua real (`ruler/gate`): tamper => revert, verify vermelho => retry,
    KPI regrediu => revert, senão accept.

    O teto de tentativas é do grafo, não do combinador: o retry da régua vira
    escalate quando o budget acaba. Toggles desligados em `config/graph.toml`
    reproduzem o stub antigo (sem KPI, sem prova de tamper).
    """
    verdict = state.get("verdict")
    attempt = state.get("attempt", 0)
    max_attempts = state["budget"].max_attempts
    policy = load_policy()

    prov: dict = {}
    if policy.tamper or policy.measure:
        prov = store.get_node(state["run_id"], "provision", _db(config)) or {}

    violations: list[str] = []
    if policy.tamper and prov.get("tamper_fp") and prov.get("genome"):
        # Genoma do payload do provision, não do workspace: a cópia lá dentro
        # é justamente o que está sob suspeita.
        gsaved = prov["genome"]
        g = Genome(
            immutable=tuple(gsaved["immutable"]),
            mutable=tuple(gsaved.get("mutable") or ()),
        )
        changed = state["exec"].files_changed if state.get("exec") else ()
        violations = tamper.detect(
            Path(state["workspace"]), prov["tamper_fp"], changed, genome=g
        )

    if verdict is None:
        # Nada verificou: só cabe tentar de novo (mesmo caminho do stub).
        action, reason = "retry", "sem_verdict"
    else:
        specs = _specs_from_payload(prov.get("kpi_specs")) if policy.measure else None
        ruled = ruler_gate(
            verdict,
            state.get("kpi_before") or {},
            state.get("kpi_after") or {},
            violations,
            specs,
        )
        action, reason = ruled.action, ruled.reason
    if action == "retry" and attempt + 1 >= max_attempts:
        action = "escalate_human"
        reason = f"{reason}; acabaram as {max_attempts} tentativas"
    # Governor: prazo estourado transforma retry em escalate — nova tentativa
    # depois do cutoff seria o loop pagando pra não entregar.
    if action == "retry" and _gov_deadline(state["run_id"], _db(config)) == CUTOFF:
        action = "escalate_human"
        reason = f"{reason}; governor:prazo_estourado"
    # Mesmo caminho para o teto de gasto: a tentativa seguinte custaria pelo
    # menos o que a anterior custou, e o dinheiro já acabou. Sem cost_cap_usd
    # (ou = 0) nada disso acontece.
    if action == "retry" and _gov_cost(state["run_id"], _db(config), attempt) == CUTOFF:
        action = "escalate_human"
        reason = f"{reason}; governor:custo_estourado"
    # Gate de delta: tentativa que não move a régua graduada não ganha outra.
    # Exige `[checks]` — sem eles o score é 1.0 fixo e "não subiu" não é notícia,
    # então a unidade binária segue com o retry cego de sempre, bit a bit.
    delta: dict[str, Any] = {}
    if action == "retry" and policy.delta_gate and state["unit"].checks:
        scores = _score_series(state["run_id"], _db(config), attempt)
        if _stagnations(scores) >= DELTA_STAGNATIONS:
            action = "escalate_human"
            reason = f"{reason}; {DELTA_REASON}"
            # Os dois scores viajam no evento porque é o que basta para quem lê a
            # parada: "a nota não passou de X" é a evidência do motivo. O
            # `escalate_reason` é o vocabulário fechado do `esc.REASONS` (o texto
            # `sem_gradiente_de_score` é detalhe do gate, não motivo novo).
            delta = {
                "score_prev": scores[-2],
                "score_now": scores[-1],
                "stagnations": _stagnations(scores),
                "escalate_reason": _delta_escalate_reason(),
            }
    decision = Decision(action, reason)  # tipo do estado, não o do ruler

    return {
        "tamper": violations,
        "decision": decision,
        "events": [
            _event("gate", action=decision.action, reason=decision.reason, **delta)
        ],
    }


def _delta_escalate_reason() -> str:
    """`esc.NO_GRADIENT` — o motivo do vocabulário fechado mais próximo daqui.

    Import tardio como o do `prior_decisions` no `_prompt`: o vocabulário é do
    lado da escalação, e um genoma sem o módulo não pode perder o gate por isso.
    """
    try:
        from harness.improve.escalate import NO_GRADIENT

        return NO_GRADIENT
    except Exception:
        return "no_gradient"


def _accept(state: RunState, config=None) -> dict:
    # No modo unidade o workspace é o próprio artefato. Com projeto, o aceite É
    # a entrega: commit no worktree e a branch vira `harness/<unit_id>` para
    # review humano — nada de merge automático.
    unit = state["unit"]
    if not unit.project:
        return {"events": [_event("accept")]}

    run_id = state["run_id"]
    db = _db(config)
    saved = store.get_node(run_id, "accept", db)
    if saved is not None:
        return {"events": [_event("accept", branch=saved["branch"], reused=True)]}

    result = state.get("exec")
    branch, commit = deliver(
        Path(state["workspace"]),
        unit.id,
        run_id,
        cost_usd=result.cost_usd if result else None,
        # Material do run fora da entrega: log da régua e trace de ferramenta.
        exclude=(VERIFY_LOG, TRACE_FILE),
    )
    store.record_node(run_id, "accept", {"branch": branch, "commit": commit}, db)
    return {"events": [_event("accept", branch=branch, commit=commit)]}


def _retry(state: RunState, config=None) -> dict:
    """Nova tentativa: zera o resultado da anterior para ninguém ler estado velho.

    O que faz o backend rodar de novo é o `attempt` na chave do ledger; limpar
    `exec`/`verdict` aqui garante que um nó futuro não confunda a tentativa
    anterior com a corrente se o processo morrer entre `retry` e `execute`.
    """
    attempt = state["attempt"] + 1
    # Legado: run que gravou o log dentro do ws antes desta mudança. Só no
    # tmpdir — em `--repo` o worktree é do alvo e não é nosso para limpar.
    if not state["unit"].project:
        (Path(state["workspace"]) / VERIFY_LOG).unlink(missing_ok=True)
    return {
        "attempt": attempt,
        "exec": None,
        "verdict": None,
        "decision": None,
        "events": [_event("retry", attempt=attempt)],
    }


def _escalate(state: RunState, config=None) -> dict:
    # Slot do `interrupt()` (PR-9). Aqui só marca e segue para o record.
    return {"events": [_event("escalate")]}


def _revert(state: RunState, config=None) -> dict:
    # Slot do rollback (PR-4: regressão de KPI descarta o worktree).
    return {"events": [_event("revert")]}


def _record(state: RunState, config=None) -> dict:
    """Uma linha no ledger por run, mesmo que o processo tenha morrido no meio."""
    # Nó terminal de TODOS os caminhos (accept/retry esgotado/escalate/revert):
    # é aqui que servidor de vida longa morre. Não no `_verify` — o verify é
    # justamente quem pode precisar do server no ar.
    try:
        procs.kill_all(Path(state["workspace"]))
    except Exception:
        pass
    run_id = state["run_id"]
    db = _db(config)
    saved = store.get_node(run_id, "record", db)
    if saved is not None:
        return {"events": [_event("record", reused=True, row_id=saved["row_id"])]}

    unit = state["unit"]
    sel = state.get("selection")
    result = state.get("exec")
    verdict = state.get("verdict")
    decision = state.get("decision")

    plan_ev = store.get_node(run_id, "plan", db) or {}
    prov_ev = store.get_node(run_id, "provision", db) or {}
    sec_total = max(0.0, time.time() - float(plan_ev.get("started_ts", time.time())))

    if result is not None and not result.ok:
        exit_reason = result.exit_reason
    elif decision is not None and decision.action == "revert":
        # paridade com o cli: revert carrega kpi_regression:… / tamper:…
        exit_reason = decision.reason
    elif verdict is not None and not verdict.passed:
        exit_reason = "verify_failed"
    else:
        exit_reason = "done"
    # `ok` é o veredito da régua, não a opinião do agente.
    ok = bool(decision and decision.action == "accept")

    row_id, wrote = store.record_run_once(
        RunRow(
            run_id=run_id,
            unit_id=unit.id,
            project=unit.project,
            backend=sel.backend if sel else "unknown",
            model=(sel.model or None) if sel else None,
            tier=sel.tier if sel else None,
            kind=sel.kind if sel else unit.kind,
            ok=ok,
            exit_reason=exit_reason,
            sec_total=sec_total,
            sec_provision=float(prov_ev.get("sec", 0.0)),
            cost_usd=result.cost_usd if result else None,
            # `intervention` só vira True com humano no loop — PR-9 (interrupt).
            intervention=False,
            created_at=store.now_iso(),
        ),
        path=db,
    )
    return {"events": [_event("record", row_id=row_id, ok=ok, reused=not wrote)]}


def _after_gate(state: RunState) -> str:
    action = state["decision"].action
    return {
        "accept": "accept",
        "retry": "retry",
        "revert": "revert",
        "escalate_human": "escalate",
    }[action]


# --- montagem -----------------------------------------------------------------


def build_run_graph(checkpointer):
    """Compila a topologia. Os nós são código imutável no genoma; a ESTRUTURA
    é declarável em `config/topology.toml` (mutável) — spec válida vale,
    qualquer falha cai na topologia embutida abaixo (fallback de 1 linha)."""
    import sys

    from langgraph.graph import END, START, StateGraph

    from harness.graph import topology

    try:
        return topology.compile_spec(topology.load_spec(), checkpointer)
    except Exception as exc:
        print(
            f"run_graph: topology.toml ignorado ({exc}); topologia embutida",
            file=sys.stderr,
        )

    b = StateGraph(RunState)
    for name, fn in (
        ("plan", _plan),
        ("route", _route),
        ("provision", _provision),
        ("execute", _execute),
        ("verify", _verify),
        ("measure", _measure),
        ("gate", _gate),
        ("accept", _accept),
        ("retry", _retry),
        ("escalate", _escalate),
        ("revert", _revert),
        ("record", _record),
    ):
        b.add_node(name, fn)

    b.add_edge(START, "plan")
    b.add_edge("plan", "route")
    b.add_edge("route", "provision")
    b.add_edge("provision", "execute")
    b.add_edge("execute", "verify")
    b.add_edge("verify", "measure")
    b.add_edge("measure", "gate")
    b.add_conditional_edges(
        "gate", _after_gate, ["accept", "retry", "escalate", "revert"]
    )
    b.add_edge("retry", "route")
    b.add_edge("accept", "record")
    b.add_edge("escalate", "record")
    b.add_edge("revert", "record")
    b.add_edge("record", END)
    return b.compile(checkpointer=checkpointer)


def dispose_project_worktree(final: RunState, data_dir: Path) -> bool:
    """Poda pós-run do modo projeto: worktree fora, repo intocado.

    No accept a branch de entrega FICA (é o artefato); em revert/escalate a
    branch efêmera do run morre junto — o repo volta a não ter vestígio do run.
    Só apaga dentro de `<data_dir>/ws`; fora dali, recusa em silêncio.
    """
    decision = final.get("decision")
    ws_raw = final.get("workspace")
    if decision is None or not ws_raw:
        return False
    target = Path(ws_raw).resolve()
    if (Path(data_dir) / "ws").resolve() not in target.parents:
        return False
    proj = get_project(final["unit"].project)
    remove_worktree(proj.repo, target)
    if decision.action != "accept":
        discard_run_branch(proj.repo, final["run_id"])
    return True


def initial_state(unit: UnitSpec, run_id: str, max_attempts: int) -> RunState:
    return RunState(
        run_id=run_id,
        unit=unit,
        attempt=0,
        selection=None,
        workspace=None,
        exec=None,
        verdict=None,
        kpi_before={},
        kpi_after={},
        tamper=[],
        decision=None,
        budget=Budget(max_attempts=max_attempts),
        reflect_hint="",
        events=[],
    )


def run_unit(
    unit_dir: Path,
    backend: str | None,
    model: str | None,
    data_dir: Path,
    thread_id: str,
    max_attempts: int | None = None,
    route: str = ROUTE_MANUAL,
) -> RunState:
    """Roda uma unidade ponta a ponta. Mesmo `thread_id` + `data_dir` = retomada.

    `max_attempts=None` lê o teto de `config/graph.toml` — é o caminho pelo
    qual o loop calibra o retry sem tocar em código.
    `route="auto"` entrega backend/model/tier ao router e por isso é excludente
    com `backend`: aceitar os dois e ignorar um em silêncio seria mentir sobre
    quem executou.
    """
    # Import tardio: o cli vai chamar o grafo, então o grafo não importa o cli
    # no topo (ciclo).
    from harness.cli import load_unit

    if route not in ROUTE_MODES:
        raise ValueError(f"route precisa ser um de {list(ROUTE_MODES)}: {route!r}")
    if route == ROUTE_AUTO and (backend or model):
        raise ValueError("route='auto': quem escolhe backend/model é o router")
    if route == ROUTE_MANUAL and not backend:
        raise ValueError("route='manual' exige backend")
    if max_attempts is None:
        max_attempts = load_policy().max_attempts

    unit = load_unit(Path(unit_dir))
    if unit.project:
        proj = get_project(unit.project)  # falha cedo se não registrado
        if proj.build_cmd:
            # O verify do projeto é build + verify da unidade, no worktree.
            unit = replace(
                unit, verify_cmd=f"({proj.build_cmd}) && ({unit.verify_cmd})"
            )
    data_dir = Path(data_dir)

    with open_checkpointer(data_dir) as checkpointer:
        from harness.graph import by_kind  # import tardio: run_unit é o único consumidor

        graph = by_kind.build_for_unit(unit, checkpointer)
        config = {
            "configurable": {
                "thread_id": thread_id,
                CFG_DATA_DIR: str(data_dir),
                CFG_BACKEND: backend,
                CFG_MODEL: model,
                CFG_MAX_TURNS: DEFAULT_MAX_TURNS,
                CFG_ROUTE: route,
            },
            # Cada tentativa gasta ~8 supersteps; sobra folga para o loop de retry.
            "recursion_limit": 12 * (max_attempts + 1),
        }
        # `next` não vazio = thread parou no meio: retoma sem reinjetar entrada.
        pending = bool(graph.get_state(config).next)
        payload = None if pending else initial_state(unit, thread_id, max_attempts)
        final = graph.invoke(payload, config)
    # Worktree de projeto é transitório: o artefato é a branch, não o checkout.
    if unit.project:
        dispose_project_worktree(final, data_dir)
    return final
