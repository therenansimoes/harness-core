"""autopilot_graph: a topologia de UM ciclo de auto-melhoria.

    pick_target → propose → genome_check → apply → fanout_ab → score
                                                                 ↓
              KEEP: commit_cfg ──┐   DISCARD/INCONCLUSIVE: revert_cfg ──┐
                                 └────── attribute → record ────────────┘
                                                       ↓
                                        próximo ciclo, ou END

Qualquer nó pode desviar para `escalate`, que é `interrupt()` do LangGraph: o
grafo PARA e o payload (`improve/escalate.py`) atravessa a parada. Quatro
causas — sem gradiente, violação de genoma, deadline, erro. A primeira é o
risco 5 da SPEC: sem mutação que valha a pena, o loop chama o humano em vez de
inventar mutação.

**Fan-out é sequencial neste PR, de propósito.** `run_ab` alterna A,B,A,B e os
braços diferem por uma mutação em `config/*.toml`, que é estado GLOBAL do
processo: dois braços em paralelo leriam o mesmo arquivo e mediriam a mesma
coisa. Então o braço é montado em duas etapas — `before_run` liga/desliga a
mutação e `spec_of` relê a config JÁ nesse estado para dizer quem executa —, e
`Budget.max_parallel` fica em 1. O `Send` paralelo da SPEC §3 entra quando a
config for injetável por run (parâmetro do `ExecRequest` em vez de arquivo) —
aí o teto do semáforo passa a valer de verdade.

Durante o ciclo, `$HARNESS_CONFIG_DIR` aponta para `<raiz>/config`: o loop muta
uma árvore e o router lê a que o env manda, e as duas têm que ser a mesma.

`deadline_ts` é checado na entrada de cada nó E antes de cada run do A/B (risco
6). Só a entrada do nó não basta: `fanout_ab` é UM nó que roda `2 x n x
unidades` runs, então um deadline de 5 minutos só seria olhado depois da hora e
meia do experimento inteiro — deadline que não interrompe nada. Estourando no
meio, o experimento é ABORTADO: os braços parciais são descartados (braço com N
diferente é amostra envenenada, não amostra pequena), a mutação volta na hora e
o loop escala com `reason="deadline"`.
"""

from __future__ import annotations

import contextlib
import operator
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Sequence, TypedDict

from harness.graph.checkpoint import open_checkpointer
from harness.graph.run_graph import CFG_BACKEND, CFG_DATA_DIR, CFG_MODEL, _cfg, _event
from harness.graph.state import Budget, Event
from harness.improve import CONFIG_SUBDIR, mutate
from harness.improve import escalate as esc
from harness.improve.mutate import GenomeViolation, Mutation, MutationError
from harness.improve.target import (
    ABORTED,
    REJECTED,
    Rule,
    Target,
    load_catalog,
    pick_target,
    with_ledger_priors,
)
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.ruler.wilson import MIN_N, Arm, decide_ab, wilson_interval
from harness.types import MutationRow

CFG_ROOT = "harness_root"
CFG_N = "harness_n_per_arm"

# Valor de `langgraph.graph.END`. Literal porque os roteadores são de módulo e
# o import do vendor é tardio (o bootstrap de env precisa rodar antes dele).
END_NODE = "__end__"


class _DeadlineHit(Exception):
    """Deadline estourado ENTRE duas runs do A/B. Nasce e morre no `fanout_ab`:
    é o único jeito de o `before_run` (que roda dentro do `run_ab`) interromper
    o experimento sem que o A/B precise conhecer o conceito de orçamento."""


class AutopilotState(TypedDict):
    """Estado do loop. Tudo que atravessa o checkpoint é escalar, lista ou dict:
    `Budget` é a única classe, e ela está na allowlist do msgpack estrito."""

    cycle: int
    cycles: int
    units: list[str]
    target: dict | None
    mutation: dict | None
    arms: dict | None
    verdict: str | None
    escalation: dict | None
    forced_rule_id: str | None
    interventions: int
    aborted: bool
    budget: Budget
    results: Annotated[list[dict], operator.add]
    events: Annotated[list[Event], operator.add]


@dataclass(frozen=True)
class AutopilotReport:
    """O que o ciclo (ou a série de ciclos) produziu.

    `intervention_rate` vem junto com `runs_window`: taxa sem N não é evidência.
    """

    thread_id: str
    cycles: int
    results: tuple[dict, ...]
    escalation: dict | None
    interventions: int
    intervention_rate: float
    runs_window: int


# --- helpers -------------------------------------------------------------------


def _db(config) -> Path:
    return Path(_cfg(config, CFG_DATA_DIR, "data")) / store.DB_NAME


def _root(config) -> Path:
    return Path(_cfg(config, CFG_ROOT, "."))


def _rule_of(state: AutopilotState, config) -> Rule:
    """Recarrega a regra do catálogo a cada nó em vez de carregá-la no estado.

    O catálogo é mutável no genoma: quem o editou entre um nó e outro (humano,
    durante uma escalação) tem que ser obedecido, e regra congelada no
    checkpoint aplicaria a versão de ontem.
    """
    rule_id = (state.get("target") or {}).get("rule_id")
    rules, _ = load_catalog(root=_root(config))
    for rule in rules:
        if rule.id == rule_id:
            return rule
    raise MutationError(f"regra sumiu do catálogo: {rule_id!r}")


def _target_dict(target: Target) -> dict:
    return {
        "rule_id": target.rule.id,
        "target_file": target.rule.target_file,
        "key": target.rule.key,
        "from": target.rule.from_value,
        "to": target.rule.to_value,
        "hypothesis": target.rule.hypothesis,
        "pattern": target.pattern,
        "freq": target.freq,
        "avg_cost": target.avg_cost,
        "prior": target.prior,
        "gain": target.gain,
        "reasons": list(target.reasons),
    }


def _stop(state: AutopilotState, reason: str, evidence: dict) -> dict:
    """Update que desvia para o `escalate` sem tocar em mais nada."""
    payload = esc.payload(
        reason,
        unit=state["units"],
        mutation=state.get("mutation"),
        evidence=evidence,
    )
    # `evidence` aninhada, não espalhada: ela traz chave livre (inclusive
    # "node"), e chave livre colidindo com o campo do evento é bug de trace.
    return {
        "escalation": payload,
        "events": [_event("escalate", reason=reason, evidence=evidence)],
    }


def _expired(state: AutopilotState, node: str) -> dict | None:
    """Checagem de deadline da entrada do nó. None = pode seguir."""
    budget = state["budget"]
    if not budget.expired(time.time()):
        return None
    return _stop(state, esc.DEADLINE, {"node": node, "deadline_ts": budget.deadline_ts})


@contextlib.contextmanager
def _pinned_config(base: Path) -> Iterator[None]:
    """`$HARNESS_CONFIG_DIR` = `<raiz do ciclo>/config` enquanto o ciclo roda.

    O loop muta `ROOT/config/*.toml` e o router lê `$HARNESS_CONFIG_DIR`: com
    os dois apontando para árvores diferentes, o ciclo calibraria uma e mediria
    a outra — exatamente o que o docstring de `improve/__init__.py` promete que
    não acontece. Restaura no fim porque o processo é o mesmo (a CLI pode ter
    outros comandos depois, e o teste tem outros testes depois).
    """
    previous = os.environ.get(CONFIG_DIR_ENV)
    os.environ[CONFIG_DIR_ENV] = str(base / CONFIG_SUBDIR)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CONFIG_DIR_ENV, None)
        else:
            os.environ[CONFIG_DIR_ENV] = previous


def _arm_text(arm: Arm) -> str:
    return f"{arm.succ}/{arm.n}"


def _arms(state: AutopilotState) -> tuple[Arm, Arm]:
    raw = state.get("arms") or {"a": [0, 0], "b": [0, 0]}
    return Arm(*raw["a"]), Arm(*raw["b"])


# --- nós ------------------------------------------------------------------------


def _pick_target(state: AutopilotState, config=None) -> dict:
    """Escolhe a mutação do ciclo, ou escala. Nada é escrito aqui."""
    if (stop := _expired(state, "pick_target")) is not None:
        return stop

    root = _root(config)
    rules, cfg = load_catalog(root=root)
    db = _db(config)
    history = store.history(limit=int(cfg["window"]), path=db)

    forced = state.get("forced_rule_id")
    if forced:
        # Rota do humano: ele já decidiu, o ganho esperado não vota.
        chosen = next((r for r in rules if r.id == forced), None)
        if chosen is None:
            return _stop(state, esc.ERROR, {"forced_rule_id": forced, "found": 0})
        target = Target(
            rule=chosen, pattern="human", freq=0.0, avg_cost=0.0,
            prior=chosen.prior(), gain=0.0, reasons=("human:forced",),
        )
    else:
        applicable = _applicable(rules, root, db)
        target = pick_target(history, applicable, cfg)
        if target is None:
            return _stop(
                state,
                esc.NO_GRADIENT,
                {
                    "history": len(history),
                    "catalog": len(rules),
                    "applicable": len(applicable),
                },
            )

    return {
        "target": _target_dict(target),
        "forced_rule_id": None,
        "escalation": None,
        "events": [
            _event(
                "pick_target",
                rule=target.rule.id,
                gain=target.gain,
                pattern=target.pattern,
            )
        ],
    }


def _applicable(rules: Sequence[Rule], root: Path, db: Path) -> list[Rule]:
    """Regras que ainda descrevem o arquivo de hoje e ainda não foram reprovadas.

    Filtro fora de `pick_target` porque `pick_target` é função pura sobre
    histórico — quem toca disco é o nó.
    """
    out: list[Rule] = []
    for rule in with_ledger_priors(rules, store.mutations(path=db)):
        try:
            current = mutate.read_value(root / rule.target_file, rule.key)
        except (OSError, MutationError):
            continue          # regra apontando pra chave que não existe mais
        if current == rule.from_value:
            out.append(rule)
    return out


def _propose(state: AutopilotState, config=None) -> dict:
    """Materializa a proposta: arquivo, chave, de/para. Ainda sem escrever."""
    if (stop := _expired(state, "propose")) is not None:
        return stop
    try:
        rule = _rule_of(state, config)
        current = mutate.read_value(_root(config) / rule.target_file, rule.key)
    except (OSError, MutationError) as exc:
        return _stop(state, esc.ERROR, {"error": str(exc)})
    if current != rule.from_value:
        return _stop(
            state,
            esc.ERROR,
            {"error": "catálogo desatualizado", "key": rule.key,
             "current": repr(current), "expected": repr(rule.from_value)},
        )
    return {
        "events": [
            _event("propose", rule=rule.id, key=rule.key,
                   change=f"{rule.from_value!r}->{rule.to_value!r}")
        ]
    }


def _genome_check(state: AutopilotState, config=None) -> dict:
    """Fail-closed antes de escrever. Rejeição vira linha no ledger: violação
    que só existe no log some no próximo `rm -rf`."""
    if (stop := _expired(state, "genome_check")) is not None:
        return stop
    try:
        rule = _rule_of(state, config)
        violations = mutate.check(rule, root=_root(config))
    except (OSError, MutationError, ValueError) as exc:
        return _stop(state, esc.ERROR, {"error": str(exc)})
    if not violations:
        return {"events": [_event("genome_check", rule=rule.id, violations=0)]}

    ts = store.now_iso()
    mid = mutate.mutation_id(rule.id, ts)
    store.record_mutation(
        MutationRow(
            mutation_id=mid,
            rule_id=rule.id,
            verdict=REJECTED,
            arm_a="0/0",
            arm_b="0/0",
            applied_at=ts,
            reverted=False,
            note=";".join(violations),
        ),
        path=_db(config),
    )
    stop = _stop(
        state, esc.GENOME_VIOLATION,
        {"rule": rule.id, "target_file": rule.target_file, "violations": len(violations)},
    )
    stop["results"] = [{
        "cycle": state["cycle"],
        "rule_id": rule.id,
        "mutation_id": mid,
        "verdict": REJECTED,
        "arm_a": "0/0",
        "arm_b": "0/0",
        "delta": None,
        "reverted": False,
        "note": ";".join(violations),
    }]
    stop["aborted"] = True
    return stop


def _apply(state: AutopilotState, config=None) -> dict:
    """Escreve a mutação. Depois deste nó o repo está sujo até commit/revert."""
    if (stop := _expired(state, "apply")) is not None:
        return stop
    try:
        rule = _rule_of(state, config)
        mutation = mutate.apply(rule, store.now_iso(), root=_root(config))
    except GenomeViolation as exc:      # cinto e suspensório do genome_check
        return _stop(state, esc.GENOME_VIOLATION, {"violations": len(exc.violations)})
    except (OSError, MutationError) as exc:
        return _stop(state, esc.ERROR, {"error": str(exc)})
    return {
        "mutation": asdict(mutation),
        "events": [_event("apply", mutation=mutation.mutation_id, rule=mutation.rule_id)],
    }


def _fanout_ab(state: AutopilotState, config=None) -> dict:
    """A/B por unidade: braço A sem a mutação, braço B com ela.

    A mutação já está aplicada quando este nó começa; o `before_run` desliga
    para o braço A e liga para o B, run a run. Isso preserva a alternância do
    `run_ab` (ambiente que degrada no meio pune os dois braços igual) mesmo com
    a config sendo estado global.

    O braço em si é montado DEPOIS do toggle, pelo `spec_of`: o router lê o
    `models.toml` recém-ligado (ou desligado) e é dele que saem
    backend/model/tier/max_turns da run. Sem isso o experimento monta os dois
    braços com a mesma `ArmSpec` congelada na entrada do nó e mede a mutação
    contra ela mesma — INCONCLUSIVE garantido, seja qual for a mutação.

    O mesmo `before_run` é onde o deadline vira interrupção de verdade: a checagem
    de entrada de nó não olharia o relógio de novo antes de `2 x n x unidades`
    runs. Cada run tem timeout próprio, então granularidade entre-runs basta.
    """
    if (stop := _expired(state, "fanout_ab")) is not None:
        return stop

    from harness.ab import ArmSpec, run_ab
    from harness.cli import load_unit
    from harness.routing import router

    mutation = Mutation(**state["mutation"])
    root = _root(config)
    n = int(_cfg(config, CFG_N, MIN_N))
    forced_backend = _cfg(config, CFG_BACKEND)
    forced_model = _cfg(config, CFG_MODEL)
    models_toml = root / CONFIG_SUBDIR / router.MODELS_FILE
    data_dir = Path(_cfg(config, CFG_DATA_DIR, "data"))
    parallel = max(1, int(state["budget"].max_parallel))
    # Histórico congelado na entrada do nó: o prior do router é função do
    # ledger, e o A/B escreve no ledger a cada run. Relendo run a run, o braço
    # poderia trocar de tier no meio do experimento por evidência que a própria
    # amostra produziu — diferença entre os braços que não é a mutação.
    history = store.history(path=_db(config))

    def before_run(label: str, _i: int) -> None:
        if state["budget"].expired(time.time()):
            raise _DeadlineHit()
        mutate.toggle(mutation, root=root, applied=label == "b")

    def spec_for(unit_spec) -> ArmSpec:
        """O braço desta run, lido da config que está no disco AGORA."""
        sel = router.select(unit_spec, history, cfg=router.load_config(models_toml))
        if forced_backend:
            # Executor no dedo (`improve --backend`): só backend/model saem da
            # mão do router. Tier e max_turns continuam vindo da config, senão
            # a mutação não teria por onde mudar a run.
            return ArmSpec(forced_backend, forced_model, sel.tier, sel.max_turns)
        return ArmSpec(sel.backend, sel.model or None, sel.tier, sel.max_turns)

    totals = {"a": Arm(0, 0), "b": Arm(0, 0)}
    try:
        for unit in state["units"]:
            unit_spec = load_unit(Path(unit))
            report = run_ab(
                unit, n=n, data_dir=data_dir,
                before_run=before_run,
                # O rótulo do braço não entra na conta: quem diz A de B é o
                # estado do toml que o `before_run` acabou de deixar no disco.
                spec_of=lambda _label, u=unit_spec: spec_for(u),
                intervention=state["interventions"] > 0,
            )
            for label, arm in (("a", report.arm_a), ("b", report.arm_b)):
                totals[label] = Arm(
                    totals[label].succ + arm.succ, totals[label].n + arm.n
                )
    except _DeadlineHit:
        # Reverte AQUI, não no `revert_cfg` de depois do resume: o processo pode
        # devolver o controle ao humano e ficar parado por dias, e a árvore não
        # fica calibrada esse tempo todo por uma mutação que ninguém mediu. O
        # `revert_cfg` do pós-resume é idempotente e só confirma.
        try:
            mutate.revert(mutation, root=root)
            revert_error = None
        except (OSError, MutationError) as exc:
            revert_error = str(exc)
        return _stop(state, esc.DEADLINE, {
            "node": "fanout_ab",
            "deadline_ts": state["budget"].deadline_ts,
            "reverted": revert_error is None,
            "revert_error": revert_error,
        })
    except Exception as exc:   # preflight, unit ilegível, backend explodindo
        return _stop(state, esc.ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    return {
        "arms": {"a": [totals["a"].succ, totals["a"].n],
                 "b": [totals["b"].succ, totals["b"].n]},
        "events": [
            _event(
                "fanout_ab",
                units=len(state["units"]),
                n=n,
                parallel=parallel,
                sequential=True,   # config global: ver docstring do módulo
                a=_arm_text(totals["a"]),
                b=_arm_text(totals["b"]),
            )
        ],
    }


def _score(state: AutopilotState, config=None) -> dict:
    """A régua fala. Nada aqui além de `decide_ab` — é o ponto do repo inteiro."""
    arm_a, arm_b = _arms(state)
    verdict = decide_ab(arm_a, arm_b)
    return {
        "verdict": verdict,
        "events": [
            _event("score", verdict=verdict, a=_arm_text(arm_a), b=_arm_text(arm_b))
        ],
    }


def _commit_cfg(state: AutopilotState, config=None) -> dict:
    """KEEP: a mutação FICA no toml. Nada a escrever — ela já está lá."""
    return {"events": [_event("commit_cfg", mutation=state["mutation"]["mutation_id"])]}


def _revert_cfg(state: AutopilotState, config=None) -> dict:
    """DISCARD/INCONCLUSIVE (ou aborto): o toml volta byte-idêntico."""
    mutation = Mutation(**state["mutation"])
    try:
        mutate.revert(mutation, root=_root(config))
        error = None
    except (OSError, MutationError) as exc:
        # Revert que falha em silêncio é pior que não reverter: o ledger diria
        # `reverted` e o arquivo continuaria mudado.
        error = str(exc)
    return {
        "events": [
            _event("revert_cfg", mutation=mutation.mutation_id, error=error)
        ]
    }


def _attribute(state: AutopilotState, config=None) -> dict:
    """Delta entre os braços com IC. Atribuição de verdade (por replay do
    histórico) é o PR-10; aqui é a conta que a própria amostra permite."""
    arm_a, arm_b = _arms(state)
    rate_a = arm_a.succ / arm_a.n if arm_a.n else 0.0
    rate_b = arm_b.succ / arm_b.n if arm_b.n else 0.0
    return {
        "events": [
            _event(
                "attribute",
                delta=rate_b - rate_a,
                ci_a=list(wilson_interval(arm_a.succ, arm_a.n)),
                ci_b=list(wilson_interval(arm_b.succ, arm_b.n)),
            )
        ]
    }


def _record(state: AutopilotState, config=None) -> dict:
    """Uma linha em `mutations` por experimento — a fonte do replay do PR-10."""
    mutation = Mutation(**state["mutation"])
    arm_a, arm_b = _arms(state)
    # Sem veredito aqui só se chegou pelo `escalate` (deadline, erro, humano
    # abortando): experimento sem amostra não é empate, é experimento que não
    # aconteceu. O `INSERT OR IGNORE` do ledger já pode ter a linha ABORTED
    # gravada pelo `escalate` — esta é a mesma linha, não outra.
    verdict = state.get("verdict") or ABORTED
    reverted = verdict != "KEEP"
    rate_a = arm_a.succ / arm_a.n if arm_a.n else 0.0
    rate_b = arm_b.succ / arm_b.n if arm_b.n else 0.0
    target = state.get("target") or {}
    # Ciclo interrompido no meio grava a mutação com o motivo — experimento
    # abortado não é experimento inexistente.
    note = (state.get("escalation") or {}).get("reason")

    store.record_mutation(
        MutationRow(
            mutation_id=mutation.mutation_id,
            rule_id=mutation.rule_id,
            verdict=verdict,
            arm_a=_arm_text(arm_a),
            arm_b=_arm_text(arm_b),
            applied_at=mutation.applied_at,
            reverted=reverted,
            note=note,
        ),
        path=_db(config),
    )
    return {
        "cycle": state["cycle"] + 1,
        "target": None,
        "mutation": None,
        "arms": None,
        "verdict": None,
        "results": [{
            "cycle": state["cycle"],
            "rule_id": mutation.rule_id,
            "mutation_id": mutation.mutation_id,
            "key": mutation.key,
            "change": f"{mutation.before_raw}->{mutation.after_raw}",
            "verdict": verdict,
            "arm_a": _arm_text(arm_a),
            "arm_b": _arm_text(arm_b),
            "delta": rate_b - rate_a,
            "gain": target.get("gain"),
            "pattern": target.get("pattern"),
            "reverted": reverted,
            "note": note,
        }],
        "events": [_event("record", mutation=mutation.mutation_id, verdict=verdict)],
    }


def _record_aborted(state: AutopilotState, config=None) -> None:
    """Linha de ledger do experimento que parou no meio.

    Grava ANTES do `interrupt()` porque a parada pode ser definitiva: quem roda
    `harness improve` na CLI e não responde nunca chega ao nó `record`, e a
    mutação teria sido aplicada sem deixar rastro nenhum — o replay do PR-10
    não teria como saber que aquele toml esteve calibrado. O `mutation_id` é
    determinístico e o ledger é `INSERT OR IGNORE`, então o `record` de um
    resume posterior encontra a linha já lá em vez de duplicá-la.
    """
    mutation = Mutation(**state["mutation"])
    arm_a, arm_b = _arms(state)
    try:
        reverted = not mutate.is_applied(mutation, root=_root(config))
    except (OSError, MutationError):
        reverted = False      # não deu para conferir: não se afirma que voltou
    store.record_mutation(
        MutationRow(
            mutation_id=mutation.mutation_id,
            rule_id=mutation.rule_id,
            verdict=ABORTED,
            arm_a=_arm_text(arm_a),
            arm_b=_arm_text(arm_b),
            applied_at=mutation.applied_at,
            reverted=reverted,
            note=(state.get("escalation") or {}).get("reason"),
        ),
        path=_db(config),
    )


def _escalate(state: AutopilotState, config=None) -> dict:
    """`interrupt()`: o grafo para aqui até alguém responder.

    Resposta aceita: `{"action": "continue", "rule_id": ...}` para mandar o loop
    seguir com uma regra escolhida a dedo, ou qualquer outra coisa para abortar.
    Quando já existe mutação aplicada, "continue" não é oferecido — a árvore
    precisa voltar ao baseline antes de qualquer novo experimento.
    """
    from langgraph.types import interrupt

    if state.get("mutation"):
        _record_aborted(state, config)
    answer = interrupt(state["escalation"])
    action = answer.get("action") if isinstance(answer, dict) else None
    rule_id = answer.get("rule_id") if isinstance(answer, dict) else None
    resume = action == esc.CONTINUE and state.get("mutation") is None

    return {
        "interventions": state["interventions"] + 1,
        "aborted": not resume,
        "forced_rule_id": rule_id if resume else None,
        "escalation": None if resume else state["escalation"],
        "events": [_event("resume", action=action or esc.ABORT, rule=rule_id)],
    }


# --- roteamento -----------------------------------------------------------------


def _gate_to(node: str):
    """Aresta condicional padrão: segue, a não ser que alguém tenha escalado."""

    def router(state: AutopilotState) -> str:
        return "escalate" if state.get("escalation") else node

    return router


def _after_score(state: AutopilotState) -> str:
    return "commit_cfg" if state.get("verdict") == "KEEP" else "revert_cfg"


def _after_escalate(state: AutopilotState) -> str:
    if not state.get("aborted"):
        return "pick_target"
    return "revert_cfg" if state.get("mutation") else END_NODE


def _after_record(state: AutopilotState) -> str:
    if state.get("aborted") or state["cycle"] >= state["cycles"]:
        return END_NODE
    return "pick_target"


def build_autopilot_graph(checkpointer):
    """Compila a topologia. Immutable no genoma — o loop calibra toml, não nós."""
    from langgraph.graph import END, START, StateGraph

    b = StateGraph(AutopilotState)
    for name, fn in (
        ("pick_target", _pick_target),
        ("propose", _propose),
        ("genome_check", _genome_check),
        ("apply", _apply),
        ("fanout_ab", _fanout_ab),
        ("score", _score),
        ("commit_cfg", _commit_cfg),
        ("revert_cfg", _revert_cfg),
        ("attribute", _attribute),
        ("record", _record),
        ("escalate", _escalate),
    ):
        b.add_node(name, fn)

    b.add_edge(START, "pick_target")
    for src, dst in (
        ("pick_target", "propose"),
        ("propose", "genome_check"),
        ("genome_check", "apply"),
        ("apply", "fanout_ab"),
        ("fanout_ab", "score"),
    ):
        b.add_conditional_edges(src, _gate_to(dst), [dst, "escalate"])
    b.add_conditional_edges("score", _after_score, ["commit_cfg", "revert_cfg"])
    b.add_edge("commit_cfg", "attribute")
    b.add_edge("revert_cfg", "attribute")
    b.add_edge("attribute", "record")
    b.add_conditional_edges("record", _after_record, ["pick_target", END])
    b.add_conditional_edges(
        "escalate", _after_escalate, ["pick_target", "revert_cfg", END]
    )
    return b.compile(checkpointer=checkpointer)


def initial_state(
    units: Sequence[str], cycles: int, budget: Budget
) -> AutopilotState:
    return AutopilotState(
        cycle=0,
        cycles=cycles,
        units=[str(u) for u in units],
        target=None,
        mutation=None,
        arms=None,
        verdict=None,
        escalation=None,
        forced_rule_id=None,
        interventions=0,
        aborted=False,
        budget=budget,
        results=[],
        events=[],
    )


def _pending_rules(rules: Sequence[Rule], base: Path, db: Path) -> list[str]:
    """Regras cujo `to` já está no config e que nenhum KEEP no ledger explica.

    É a assinatura de um crash entre o `apply` e o `record`: o toml ficou
    calibrado por uma mutação que ninguém julgou. Começar um ciclo assim mediria
    o braço A JÁ mutado — o A/B compararia a mutação contra ela mesma e o
    veredito seria sobre nada.
    """
    kept = {m.rule_id for m in store.mutations(path=db) if m.verdict == "KEEP"}
    dirty: list[str] = []
    for rule in rules:
        if rule.id in kept:
            continue
        try:
            current = mutate.read_value(base / rule.target_file, rule.key)
        except (OSError, MutationError):
            continue          # regra apontando pra chave que não existe mais
        if current == rule.to_value:
            dirty.append(rule.id)
    return dirty


def run_autopilot(
    data_dir: Path | str,
    cycles: int = 1,
    deadline_s: float | None = None,
    *,
    units: Sequence[Path | str] = (),
    backend: str | None = None,
    model: str | None = None,
    n: int | None = None,
    root: Path | str | None = None,
    thread_id: str | None = None,
    resume: Any = None,
) -> AutopilotReport:
    """Roda `cycles` ciclos de melhoria. Mesmo `thread_id` + `data_dir` = retomada.

    `resume` não-None reinjeta a resposta do humano no `interrupt()` pendente —
    é o outro lado do `escalate`, e é por onde `intervention` entra no ledger.

    `backend` None = quem escolhe o executor é o router, lendo o `models.toml`
    da raiz do ciclo; é o default porque é o que deixa a mutação chegar até a
    run. Passar backend fixa o executor dos DOIS braços e reduz o experimento
    ao que sobra da config (tier e max_turns).
    """
    from harness.improve import root_dir

    if not units:
        raise ValueError("autopilot sem unidade de avaliação: nada a medir")
    if cycles <= 0:
        raise ValueError(f"cycles tem que ser positivo: {cycles}")

    base = root_dir(root)
    data_dir = Path(data_dir)
    rules, cfg = load_catalog(root=base)
    if thread_id is None:
        # Só na thread NOVA: numa retomada a mutação aplicada é esperada — ela
        # está no estado do checkpoint, com dono e com revert programado.
        dirty = _pending_rules(rules, base, data_dir / store.DB_NAME)
        if dirty:
            raise ValueError(
                f"mutação pendente: config sujo — reverta ou registre: {', '.join(dirty)}"
            )
    thread_id = thread_id or f"improve-{uuid.uuid4().hex[:12]}"
    budget = Budget(
        # `if deadline_s` engoliria o 0: "deadline agora" viraria "sem deadline",
        # que é o oposto exato do que quem passou 0 pediu.
        deadline_ts=time.time() + deadline_s if deadline_s is not None else None,
        max_parallel=int(cfg["max_parallel"]),
    )

    with _pinned_config(base), open_checkpointer(data_dir) as checkpointer:
        graph = build_autopilot_graph(checkpointer)
        config = {
            "configurable": {
                "thread_id": thread_id,
                CFG_DATA_DIR: str(data_dir),
                CFG_ROOT: str(base),
                CFG_BACKEND: backend,
                CFG_MODEL: model,
                CFG_N: int(n or cfg["n_per_arm"]),
            },
            # ~10 supersteps por ciclo, com folga para as escalações.
            "recursion_limit": 16 * (cycles + 1),
        }
        if resume is not None:
            from langgraph.types import Command

            payload: Any = Command(resume=resume)
        elif graph.get_state(config).next:
            payload = None            # thread parada no meio: retoma sem entrada
        else:
            payload = initial_state(units, cycles, budget)
        final = graph.invoke(payload, config)

    interrupts = final.get("__interrupt__") or ()
    window = int(cfg["window"])
    history = store.history(limit=window, path=data_dir / store.DB_NAME)
    # Parada pendente vem do `__interrupt__`; escalação já respondida (e
    # abortada) só sobrevive no estado — as duas contam como escalação.
    escalation = interrupts[0].value if interrupts else final.get("escalation")
    return AutopilotReport(
        thread_id=thread_id,
        cycles=int(final.get("cycle", 0)),
        results=tuple(final.get("results", ())),
        escalation=escalation,
        interventions=int(final.get("interventions", 0)),
        intervention_rate=esc.intervention_rate(history, window),
        runs_window=len(history),
    )
