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
import random
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from harness.graph.checkpoint import open_checkpointer
from harness.graph.run_graph import CFG_BACKEND, CFG_DATA_DIR, CFG_MODEL, _cfg, _event
from harness.graph.state import Budget, Event
from harness.improve import CONFIG_SUBDIR, meta, mutate
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
from harness.ruler import pareto
from harness.ruler.wilson import MIN_N, Arm, decide_ab, wilson_interval
from harness.types import MutationRow

CFG_ROOT = "harness_root"
CFG_N = "harness_n_per_arm"
# Exame selado do meta-check (config/ruler.toml), injetável via `configurable`.
# Default fail-closed: sem exame configurado, NADA passa — e `human_ack` no
# loop é False por construção (ver improve/meta.py), então o melhor caso do
# autopilot sozinho é quarentena, nunca aplicação.
CFG_SEALED_EXAM = "harness_sealed_exam"
# Ação de evolução fixada pelo chamador. Ausente → a policy (bandit sobre o
# histórico de mutações) escolhe entre as ações do registry.
CFG_ACTION = "harness_action"
# Chave do LangGraph, não nossa: é o `thread_id` do `configurable`.
CFG_THREAD = "thread_id"
# Relógio injetável (callable -> epoch). Default `time.time`: o teto de ciclo do
# governor se testa sem dormir uma hora, e o resto do grafo não muda de
# comportamento por causa disso.
CFG_CLOCK = "harness_clock"

# Nome do nó que escreve no config, e do marcador que ele deixa no ledger.
APPLY_NODE = "apply"

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
    # Eixos do Pareto do ciclo: {"a": {"cost_usd": …, "sec_total": …}, "b": {…}},
    # médias por run acumuladas no `fanout_ab`. Só o `score` lê.
    axes: dict
    verdict: str | None
    escalation: dict | None
    abort_reason: str | None
    forced_rule_id: str | None
    interventions: int
    aborted: bool
    budget: Budget
    # Relógio do ciclo corrente (wall clock), estampado pelo `pick_target` e
    # limpo pelo `record`: é contra ele que o `cycle_s` do governor mede. Vive no
    # checkpoint porque o resume acontece noutro processo, e ciclo cujo relógio
    # zera a cada processo não tem teto nenhum. None = ciclo sem marca (thread
    # de antes deste campo) e o teto do ciclo não corta.
    cycle_started_ts: float | None
    # Banco do governor: `"<kind>:<ação>"` (ou a ação nua, quando o ciclo não
    # tem kind) -> ciclo em que ela entrou. Chave string composta, não dict
    # aninhado: é o que atravessa o checkpointer msgpack. Vive no checkpoint
    # porque o prazo de soltura (`bench_cycles`) se conta em ciclos DESTA thread;
    # derivar do ledger daria "há quantas mutações", não "há quantos ciclos".
    bench_since: dict[str, int]
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


def _units_kind(state: AutopilotState) -> str | None:
    """Kind das unidades do ciclo, quando é UM só. Misto/ilegível/ausente → None.

    É a chave do prior por (kind, ação) do bandit: `mutations` não tem `kind`
    nem `run_id` para juntar com `runs`, então o kind vem daqui e é gravado no
    note pelo `_record`. Ciclo com unidades de kinds diferentes não tem kind:
    misturar `code` com `content` num rótulo só ensinaria a coisa errada.
    """
    from harness.cli import load_unit

    kinds = set()
    for unit in state["units"]:
        try:
            kinds.add(load_unit(Path(unit)).kind)
        except Exception:
            return None
    return kinds.pop() if len(kinds) == 1 else None


def _bench_stats(stats: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Placar da policy -> vocabulário do banco do governor (`bench`)."""
    return {n: {"proposals": s["n"], "keeps": s["keep"]} for n, s in stats.items()}


def _stop(state: AutopilotState, reason: str, evidence: dict) -> dict:
    """Update que desvia para o `escalate` sem tocar em mais nada.

    O `kind` do ciclo vai junto: é a chave da memória de casos (o precedente
    humano entra na evidência pelo `esc.payload`, e a resposta é gravada nessa
    mesma célula pelo lado da CLI).
    """
    payload = esc.payload(
        reason,
        unit=state["units"],
        mutation=state.get("mutation"),
        evidence=evidence,
        kind=_units_kind(state),
    )
    # `evidence` aninhada, não espalhada: ela traz chave livre (inclusive
    # "node"), e chave livre colidindo com o campo do evento é bug de trace.
    return {
        "escalation": payload,
        "events": [_event("escalate", reason=reason, evidence=evidence)],
    }


def _now(config=None) -> float:
    """Epoch do relógio injetado (`CFG_CLOCK`), ou o wall clock. Torto -> wall."""
    clock = _cfg(config, CFG_CLOCK, None)
    if callable(clock):
        try:
            return float(clock())
        except (TypeError, ValueError):
            return time.time()
    return time.time()


def _expired(state: AutopilotState, node: str, config=None) -> dict | None:
    """Checagem de prazo da entrada do nó. None = pode seguir.

    Dois prazos, um único ponto: o do RUN (`--deadline-s`, que vira
    `budget.deadline_ts`) e o do CICLO (`cycle_s` do governor, medido contra o
    `cycle_started_ts` estampado no `pick_target`). Os dois saem pelo mesmo
    `escalate`; o motivo do ciclo viaja na evidência porque o vocabulário de
    `esc.REASONS` é fechado e prazo estourado é `deadline` dos dois jeitos.
    """
    budget = state["budget"]
    now = _now(config)
    if budget.expired(now):
        return _stop(state, esc.DEADLINE, {"node": node, "deadline_ts": budget.deadline_ts})
    from harness.governor import governor as gov_mod

    started = state.get("cycle_started_ts")
    gov = gov_mod.load_gov()
    if gov_mod.check_cycle(started, now, gov) == gov_mod.CUTOFF:
        return _stop(
            state,
            esc.DEADLINE,
            {
                "node": node,
                "governor": "governor:ciclo_estourado",
                "cycle_s": gov.cycle_s,
                "cycle_started_ts": started,
            },
        )
    return None


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
    if (stop := _expired(state, "pick_target", config)) is not None:
        return stop

    # Relógio do ciclo, estampado uma vez só: este é o primeiro nó do ciclo, e
    # escalação que volta para cá (humano forçando regra) NÃO reinicia a marca —
    # ciclo que já queimou o `cycle_s` não ganha outro por causa da parada.
    started_ts = state.get("cycle_started_ts")
    if started_ts is None:
        started_ts = _now(config)

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
            rule=chosen,
            pattern="human",
            freq=0.0,
            avg_cost=0.0,
            prior=chosen.prior(),
            gain=0.0,
            reasons=("human:forced",),
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

    target_d = _target_dict(target)
    # Kind do ciclo: chave do prior por (kind, ação) na policy e rótulo que o
    # `_record` grava no note. Vale também com ação fixada — o ledger precisa da
    # célula preenchida para o bandit do ciclo seguinte ter o que ler.
    kind = _units_kind(state)
    # Ação de evolução do ciclo: fixada pelo chamador, ou escolhida pela
    # policy (bandit sobre o KEEP-rate por (kind, ação) no ledger, global quando
    # o kind é desconhecido). Rota forçada pelo humano fica fora do bandit: ele
    # já decidiu a regra, a policy não vota.
    action = _cfg(config, CFG_ACTION, None)
    # Banco intacto quando o bandit não vota neste ciclo (ação fixada, rota do
    # humano): ninguém cumpre pena de ciclo que não julgou ninguém.
    bench_since = dict(state.get("bench_since") or {})
    if not action and not forced:
        from harness.improve import policy
        from harness.improve import target as improve_target

        rng = random.Random(f"{_cfg(config, CFG_THREAD, '')}:{state['cycle']}")
        names = sorted(improve_target.actions())
        # Governor no bandit: (a) banco — ação que só propõe e nunca cola KEEP
        # sai da roleta (nunca esvazia: foco não é paralisia) e volta depois de
        # `bench_cycles` ciclos, com o ciclo de entrada guardado em
        # `bench_since` sob a chave `"<kind>:<ação>"` (o banco é por célula
        # quando o kind do ciclo é conhecido, como o prior do bandit); (b)
        # exploração fecha conforme os ciclos correm (explore_budget). Fail-open.
        explore = 1.0
        try:
            from harness.governor import governor as gov_mod

            gov = gov_mod.load_gov()
            mutations = list(store.mutations(path=db))
            benched, bench_since = gov_mod.bench_with_expiry(
                _bench_stats(policy.action_stats(mutations)),
                gov,
                state["cycle"],
                state.get("bench_since"),
                kind=kind,
                cell_stats=_bench_stats(policy.action_stats(mutations, kind=kind))
                if kind
                else None,
            )
            names = [n for n in names if n not in benched] or names
            explore = gov_mod.explore_budget(state["cycle"] / max(1, state["cycles"]), gov)
        except Exception:
            pass
        action = policy.select_action(
            names,
            store.mutations(path=db),
            rng,
            explore=explore,
            kind=kind,
        )
    target_d["action"] = action
    target_d["kind"] = kind

    return {
        "target": target_d,
        "forced_rule_id": None,
        "escalation": None,
        "cycle_started_ts": started_ts,
        "bench_since": bench_since,
        "events": [
            _event(
                "pick_target",
                rule=target.rule.id,
                gain=target.gain,
                pattern=target.pattern,
                action=action,
                kind=kind,
                benched=sorted(bench_since),
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
            continue  # regra apontando pra chave que não existe mais
        if current == rule.from_value:
            out.append(rule)
    return out


def _propose(state: AutopilotState, config=None) -> dict:
    """Materializa a proposta: arquivo, chave, de/para. Ainda sem escrever."""
    if (stop := _expired(state, "propose", config)) is not None:
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
            {
                "error": "catálogo desatualizado",
                "key": rule.key,
                "current": repr(current),
                "expected": repr(rule.from_value),
            },
        )
    return {
        "events": [
            _event(
                "propose",
                rule=rule.id,
                key=rule.key,
                change=f"{rule.from_value!r}->{rule.to_value!r}",
            )
        ]
    }


def _genome_check(state: AutopilotState, config=None) -> dict:
    """Fail-closed antes de escrever. Rejeição vira linha no ledger: violação
    que só existe no log some no próximo `rm -rf`."""
    if (stop := _expired(state, "genome_check", config)) is not None:
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
        state,
        esc.GENOME_VIOLATION,
        {"rule": rule.id, "target_file": rule.target_file, "violations": len(violations)},
    )
    stop["results"] = [
        {
            "cycle": state["cycle"],
            "rule_id": rule.id,
            "mutation_id": mid,
            "verdict": REJECTED,
            "arm_a": "0/0",
            "arm_b": "0/0",
            "delta": None,
            "reverted": False,
            "note": ";".join(violations),
        }
    ]
    stop["aborted"] = True
    return stop


def _default_sealed_exam(config=None):
    """Default do meta-exame quando o chamador não injeta `CFG_SEALED_EXAM`:
    o exame selado REAL, amarrado à raiz do ciclo. Import guardado porque
    `exam.py` pode não existir num checkout parcial — aí o default degrada
    para o antigo `lambda: False`. Fail-closed por construção nos dois
    braços: sealed vazio, erro ou módulo ausente → False, nada aprovado."""
    try:
        from harness.improve import exam
    except ImportError:
        return lambda: False
    root = _root(config)
    return lambda: exam.run_sealed_exam(
        sealed_dir=root / exam.SEALED_DIR,
        data_dir=_cfg(config, CFG_DATA_DIR, "data"),
    )


def _apply(state: AutopilotState, config=None) -> dict:
    """Escreve a mutação. Depois deste nó o repo está sujo até commit/revert."""
    if (stop := _expired(state, "apply", config)) is not None:
        return stop
    try:
        rule = _rule_of(state, config)
        # Meta-exame ANTES de qualquer marca ou escrita: mudança na régua
        # (config/ruler.toml) exige exame selado + ack humano, e o autopilot
        # NUNCA produz ack (human_ack=False fixo). Alvo comum => "allowed"
        # sem custo; "quarantined"/"blocked" => escalate, nada é aplicado.
        run_exam = _cfg(config, CFG_SEALED_EXAM, None) or _default_sealed_exam(config)
        meta_verdict = meta.meta_check(
            Path(rule.target_file), run_sealed_exam=run_exam, human_ack=False
        )
        if meta_verdict != meta.ALLOWED:
            return _stop(
                state,
                esc.GENOME_VIOLATION,
                {
                    "meta": meta_verdict,
                    "rule": rule.id,
                    "target_file": rule.target_file,
                },
            )
        # Marcador de "o loop vai mexer no config" ANTES da escrita, e não
        # depois: é a única evidência que sobrevive a um SIGKILL no meio do
        # ciclo, e é dela que o `_pending_rules` do próximo boot depende para
        # distinguir toml sujo de toml que sempre foi assim. A ordem importa —
        # marcar depois de mutar deixaria a janela em que a mutação existe no
        # arquivo sem existir em lugar nenhum, que é exatamente o config
        # calibrado em silêncio que o guard existe para pegar. O contrário
        # (marca sem mutação) é inofensivo: o guard confere o valor do toml
        # antes de acusar. `mutation_id` é determinístico do par (regra, ts),
        # então é o MESMO que o `record` vai gravar no fim.
        ts = store.now_iso()
        store.record_node(
            _cfg(config, CFG_THREAD, ""),
            APPLY_NODE,
            {
                "rule_id": rule.id,
                "mutation_id": mutate.mutation_id(rule.id, ts),
                "target_file": rule.target_file,
                "key": rule.key,
            },
            path=_db(config),
            # Chaveado por `(thread, ciclo)`: retomar o mesmo ciclo reaproveita
            # a marca em vez de duplicá-la.
            attempt=state["cycle"],
        )
        mutation = mutate.apply(rule, ts, root=_root(config))
    except GenomeViolation as exc:  # cinto e suspensório do genome_check
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
    if (stop := _expired(state, "fanout_ab", config)) is not None:
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
    # Eixos do Pareto somados sobre TODAS as unidades: custo com denominador
    # próprio (run que não mediu sai dos dois lados da divisão), tempo com o seu.
    acc = {
        label: {"cost_sum": 0.0, "cost_n": 0, "sec_sum": 0.0, "sec_n": 0} for label in ("a", "b")
    }
    try:
        for unit in state["units"]:
            unit_spec = load_unit(Path(unit))
            report = run_ab(
                unit,
                n=n,
                data_dir=data_dir,
                before_run=before_run,
                # O rótulo do braço não entra na conta: quem diz A de B é o
                # estado do toml que o `before_run` acabou de deixar no disco.
                spec_of=lambda _label, u=unit_spec: spec_for(u),
                intervention=state["interventions"] > 0,
            )
            for label, arm in (("a", report.arm_a), ("b", report.arm_b)):
                totals[label] = Arm(totals[label].succ + arm.succ, totals[label].n + arm.n)
            for label, arm_rows in (("a", report.rows_a), ("b", report.rows_b)):
                bucket = acc[label]
                for row in arm_rows:
                    if row.cost_usd is not None:
                        bucket["cost_sum"] += float(row.cost_usd)
                        bucket["cost_n"] += 1
                    bucket["sec_sum"] += float(row.sec_total)
                    bucket["sec_n"] += 1
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
        return _stop(
            state,
            esc.DEADLINE,
            {
                "node": "fanout_ab",
                "deadline_ts": state["budget"].deadline_ts,
                "reverted": revert_error is None,
                "revert_error": revert_error,
            },
        )
    except Exception as exc:  # preflight, unit ilegível, backend explodindo
        return _stop(state, esc.ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    axes = {label: _mean_axes(acc[label]) for label in ("a", "b")}
    return {
        "arms": {"a": [totals["a"].succ, totals["a"].n], "b": [totals["b"].succ, totals["b"].n]},
        "axes": axes,
        "events": [
            _event(
                "fanout_ab",
                units=len(state["units"]),
                n=n,
                parallel=parallel,
                sequential=True,  # config global: ver docstring do módulo
                a=_arm_text(totals["a"]),
                b=_arm_text(totals["b"]),
                cost_a=axes["a"]["cost_usd"],
                cost_b=axes["b"]["cost_usd"],
                sec_a=axes["a"]["sec_total"],
                sec_b=axes["b"]["sec_total"],
            )
        ],
    }


def _mean_axes(bucket: dict) -> dict:
    """Soma acumulada -> média por run. `None` = nenhuma run mediu o eixo."""
    return {
        "cost_usd": bucket["cost_sum"] / bucket["cost_n"] if bucket["cost_n"] else None,
        "sec_total": bucket["sec_sum"] / bucket["sec_n"] if bucket["sec_n"] else None,
    }


def _score(state: AutopilotState, config=None) -> dict:
    """A régua fala. `decide_ab` decide qualidade; o Pareto (desligado por
    default) só pode transformar um KEEP em INCONCLUSIVE por custo/tempo."""
    arm_a, arm_b = _arms(state)
    verdict = decide_ab(arm_a, arm_b)
    axes = state.get("axes") or {}
    verdict, worse = pareto.apply(
        verdict, axes.get("a") or {}, axes.get("b") or {}, pareto.load_pareto()
    )
    extra = {"pareto": ",".join(worse)} if worse else {}
    return {
        "verdict": verdict,
        "events": [
            _event(
                "score",
                verdict=verdict,
                a=_arm_text(arm_a),
                b=_arm_text(arm_b),
                **extra,
            )
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
    return {"events": [_event("revert_cfg", mutation=mutation.mutation_id, error=error)]}


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
    # abortado não é experimento inexistente. O motivo vem do `abort_reason`
    # quando o humano já respondeu: `escalation` é o que ainda espera resposta,
    # e o `escalate` a limpa ao ser respondido.
    note = (state.get("escalation") or {}).get("reason") or state.get("abort_reason")
    # Fecha o feedback do bandit: o nome da ação escolhida no `pick_target` vai
    # para a coluna `action` do ledger e é o que a policy relê no próximo ciclo
    # para pontuar o KEEP-rate por ação. Continua também no `note`: quem já tem
    # o histórico antigo lê as duas eras pelo mesmo parse. Só em veredito
    # concluído — ABORTED não é evidência sobre a ação, e o note do aborto
    # (motivo da parada) fica intacto para o humano.
    from harness.improve import policy

    action = target.get("action")
    recorded_action = None
    if verdict in ("KEEP", "DISCARD", "INCONCLUSIVE"):
        # O kind do ciclo vai junto no note: é o que faz o prior por
        # (kind, ação) existir no ciclo seguinte. Sem coluna própria em
        # `mutations`, o note é a única casa dele.
        note = policy.note_with_action(action, note, kind=target.get("kind"))
        recorded_action = action

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
            action=recorded_action,
        ),
        path=_db(config),
    )
    return {
        "cycle": state["cycle"] + 1,
        "target": None,
        "mutation": None,
        "arms": None,
        "verdict": None,
        "abort_reason": None,
        # Ciclo fechado, relógio zerado: o próximo `pick_target` estampa o dele.
        "cycle_started_ts": None,
        "results": [
            {
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
                "action": action,
                "reverted": reverted,
                "note": note,
            }
        ],
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
        reverted = False  # não deu para conferir: não se afirma que voltou
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

    Respondida, a escalação SAI do estado nos dois casos. `escalation` significa
    "o loop está parado esperando gente": mantê-la depois da resposta faz o
    relatório do resume reimprimir no stderr um pedido de ajuda que já foi
    atendido, com o `thread=` de uma thread encerrada. O motivo continua vivo em
    `abort_reason`, que é o que vira `note` da linha ABORTED no ledger.
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
        "escalation": None,
        "abort_reason": None if resume else (state["escalation"] or {}).get("reason"),
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
    b.add_conditional_edges("escalate", _after_escalate, ["pick_target", "revert_cfg", END])
    return b.compile(checkpointer=checkpointer)


def initial_state(units: Sequence[str], cycles: int, budget: Budget) -> AutopilotState:
    return AutopilotState(
        cycle=0,
        cycles=cycles,
        units=[str(u) for u in units],
        target=None,
        mutation=None,
        arms=None,
        axes={"a": {}, "b": {}},
        verdict=None,
        escalation=None,
        abort_reason=None,
        forced_rule_id=None,
        interventions=0,
        aborted=False,
        budget=budget,
        cycle_started_ts=None,
        bench_since={},
        results=[],
        events=[],
    )


def _pending_rules(rules: Sequence[Rule], base: Path, db: Path) -> list[str]:
    """Regras cuja mutação pode ter ficado no config sem ninguém ter julgado.

    É a assinatura de um crash entre o `apply` e o `record`: o toml ficou
    calibrado por uma mutação que nenhum veredito explica. Começar um ciclo
    assim mediria o braço A JÁ mutado — o A/B compararia a mutação contra ela
    mesma e o veredito seria sobre nada.

    Duas provas, e as duas exigem que alguém TENHA aplicado: o marcador que o nó
    `apply` deixa no ledger sem linha de veredito correspondente, e a linha
    ABORTED que não diz ter voltado. O valor do toml sozinho não prova nada —
    `to` pode ser simplesmente o que o arquivo sempre teve (regra escrita contra
    uma versão antiga do config), e acusar isso de sujeira trava para sempre um
    loop cujo repo está limpo. O falso positivo custa mais que o falso negativo
    aqui: o negativo ainda esbarra no `from` conferido pelo `mutate.apply`.

    Ledger sem teto de propósito: `node_payloads` lê TODAS as marcas de `apply`,
    e casá-las com uma janela das mutações mais recentes é comparar conjuntos de
    tamanhos diferentes. Bastavam 500 mutações depois de um KEEP para o veredito
    dele sair da janela: a marca antiga viraria "apply sem veredito", a regra
    viraria suspeita, e o boot recusaria para sempre um repo limpo — o mesmo
    falso positivo que esta função existe para eliminar.
    """
    ledger = store.mutations(limit=None, path=db)
    judged = {m.mutation_id for m in ledger}
    kept = {m.rule_id for m in ledger if m.verdict == "KEEP"}
    suspect = {m.rule_id for m in ledger if m.verdict == ABORTED and not m.reverted}
    suspect |= {
        str(p.get("rule_id"))
        for p in store.node_payloads(APPLY_NODE, path=db)
        if p.get("mutation_id") not in judged
    }

    dirty: list[str] = []
    for rule in rules:
        if rule.id not in suspect or rule.id in kept:
            continue
        try:
            current = mutate.read_value(base / rule.target_file, rule.key)
        except (OSError, MutationError):
            continue  # regra apontando pra chave que não existe mais
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
    action: str | None = None,
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
                # None = a policy escolhe a ação; string = chamador fixou.
                CFG_ACTION: action,
            },
            # ~10 supersteps por ciclo, com folga para as escalações.
            "recursion_limit": 16 * (cycles + 1),
        }
        if resume is not None:
            from langgraph.types import Command

            payload: Any = Command(resume=resume)
        elif graph.get_state(config).next:
            payload = None  # thread parada no meio: retoma sem entrada
        else:
            payload = initial_state(units, cycles, budget)
        final = graph.invoke(payload, config)

    interrupts = final.get("__interrupt__") or ()
    window = int(cfg["window"])
    history = store.history(limit=window, path=data_dir / store.DB_NAME)
    # Só a parada PENDENTE conta: ela vem do `__interrupt__`. Escalação já
    # respondida sai do estado no próprio `escalate` — reimprimi-la faria a CLI
    # pedir de novo uma resposta que já veio. O que aconteceu no ciclo abortado
    # continua no `results` (verdict ABORTED + `note`).
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
