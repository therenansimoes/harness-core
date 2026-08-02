"""run_graph: a topologia de um run.

    plan → route → provision → execute → verify → measure → gate
                                                     ↓
              accept ──┐   retry → route   escalate ──┐   revert ──┐
                       └──────────── record ──────────┴────────────┘ → END

Nós `route`, `measure` e `gate` são stubs determinísticos neste PR — o router
real é PR-6 e a régua real é PR-4. O que este PR entrega de verdade é a
idempotência: toda escrita externa passa por `(run_id, node)` no ledger, então
matar o processo no meio de `execute` e reinvocar o mesmo `thread_id` retoma sem
reexecutar nada que já aconteceu.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from harness.backends import registry
from harness.graph.checkpoint import open_checkpointer
from harness.graph.state import Budget, Decision, Event, RunState
from harness.ledger import store
from harness.types import ExecRequest, ExecResult, RunRow, Selection, UnitSpec, Verdict

# Chaves nossas dentro de `config["configurable"]`. O que não é estado do run
# (para onde escrever, quem executa) viaja por aqui, não pelo checkpoint.
CFG_DATA_DIR = "harness_data_dir"
CFG_BACKEND = "harness_backend"
CFG_MODEL = "harness_model"
CFG_MAX_TURNS = "harness_max_turns"

DEFAULT_MAX_TURNS = 8
VERIFY_TIMEOUT_S = 120.0
VERIFY_LOG = "verify.log"
TRACE_FILE = "trace.jsonl"

# Não vão para o workspace: a spec da unidade e lixo de ferramenta.
IGNORE_NAMES = ("unit.toml", "__pycache__", ".git", ".venv", "node_modules")


# --- helpers de config/serialização ------------------------------------------


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
    }


def _verdict_from_payload(d: dict) -> Verdict:
    return Verdict(
        passed=bool(d["passed"]),
        exit_code=int(d["exit_code"]),
        log_path=Path(d["log_path"]),
        sec=float(d["sec"]),
    )


def _copy_unit_files(src: Path, dst: Path) -> None:
    """Materializa o dir da unidade no workspace. Sobrescreve: é idempotente."""
    ignore = shutil.ignore_patterns(*IGNORE_NAMES)
    for item in sorted(src.iterdir()):
        if item.name in IGNORE_NAMES:
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
    store.record_node(run_id, "plan", {"started_ts": time.time()}, _db(config))
    return {
        "run_id": run_id,
        "attempt": state.get("attempt", 0),
        "events": [_event("plan", run_id=run_id, unit=unit.id)],
    }


def _route(state: RunState, config=None) -> dict:
    """Stub determinístico: honra o pedido. Router com prior é PR-6."""
    unit = state["unit"]
    selection = Selection(
        backend=_cfg(config, CFG_BACKEND, "mock"),
        model=_cfg(config, CFG_MODEL) or "",
        tier="t0",
        kind=unit.kind or "code",
        max_turns=int(_cfg(config, CFG_MAX_TURNS, DEFAULT_MAX_TURNS)),
        reasons=("stub:pedido_do_chamador",),
    )
    return {
        "selection": selection,
        "events": [_event("route", backend=selection.backend, tier=selection.tier)],
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
    _copy_unit_files(state["unit"].path, ws)
    sec = time.monotonic() - t0
    store.record_node(run_id, "provision", {"workspace": str(ws), "sec": sec}, db)
    return {
        "workspace": str(ws),
        "events": [_event("provision", workspace=str(ws), reused=False)],
    }


def _execute(state: RunState, config=None) -> dict:
    """Chama o backend — no máximo uma vez por run, custe o que custar o crash."""
    run_id = state["run_id"]
    db = _db(config)
    saved = store.get_node(run_id, "execute", db)
    if saved is not None:
        return {
            "exec": _exec_from_payload(saved),
            "events": [_event("execute", reused=True)],
        }

    sel = state["selection"]
    ws = Path(state["workspace"])
    result = registry.get_backend(sel.backend).execute(
        ExecRequest(
            prompt=state["unit"].prompt,
            workspace=ws,
            model=sel.model or None,
            max_turns=sel.max_turns,
            trace_path=ws / TRACE_FILE,
        )
    )
    store.record_node(run_id, "execute", _exec_payload(result), db)
    return {
        "exec": result,
        "events": [_event("execute", ok=result.ok, exit_reason=result.exit_reason)],
    }


def _verify(state: RunState, config=None) -> dict:
    """A régua roda o `verify_cmd` da unidade. PR-4 troca isto por `ruler/verify`."""
    run_id = state["run_id"]
    db = _db(config)
    saved = store.get_node(run_id, "verify", db)
    if saved is not None:
        return {
            "verdict": _verdict_from_payload(saved),
            "events": [_event("verify", reused=True)],
        }

    ws = Path(state["workspace"])
    log_path = ws / VERIFY_LOG
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            state["unit"].verify_cmd,
            shell=True,
            cwd=ws,
            capture_output=True,
            timeout=VERIFY_TIMEOUT_S,
        )
        exit_code, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        exit_code, out = 124, b"verify: timeout\n"
    log_path.write_bytes(out)

    verdict = Verdict(
        passed=exit_code == 0,
        exit_code=exit_code,
        log_path=log_path,
        sec=time.monotonic() - t0,
    )
    store.record_node(run_id, "verify", _verdict_payload(verdict), db)
    return {
        "verdict": verdict,
        "events": [_event("verify", passed=verdict.passed, exit_code=exit_code)],
    }


def _measure(state: RunState, config=None) -> dict:
    """Stub: coleta de KPI é PR-4. Os campos existem para o gate não mudar depois."""
    return {"kpi_before": {}, "kpi_after": {}, "events": [_event("measure")]}


def _gate(state: RunState, config=None) -> dict:
    """Stub da régua: veredito manda, tamper é vazio até PR-5."""
    verdict = state.get("verdict")
    attempt = state.get("attempt", 0)
    max_attempts = state["budget"].max_attempts

    if verdict is not None and verdict.passed:
        decision = Decision("accept", "verify passou")
    elif attempt + 1 < max_attempts:
        decision = Decision("retry", f"verify falhou, tentativa {attempt + 1}")
    else:
        decision = Decision("escalate_human", "verify falhou e acabaram as tentativas")

    return {
        "tamper": [],
        "decision": decision,
        "events": [_event("gate", action=decision.action, reason=decision.reason)],
    }


def _accept(state: RunState, config=None) -> dict:
    # Slot do commit (PR-3 dá o worktree, PR-4 a régua completa).
    return {"events": [_event("accept")]}


def _retry(state: RunState, config=None) -> dict:
    return {"attempt": state["attempt"] + 1, "events": [_event("retry")]}


def _escalate(state: RunState, config=None) -> dict:
    # Slot do `interrupt()` (PR-9). Aqui só marca e segue para o record.
    return {"events": [_event("escalate")]}


def _revert(state: RunState, config=None) -> dict:
    # Slot do rollback (PR-4: regressão de KPI descarta o worktree).
    return {"events": [_event("revert")]}


def _record(state: RunState, config=None) -> dict:
    """Uma linha no ledger por run, mesmo que o processo tenha morrido no meio."""
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
    elif verdict is not None and not verdict.passed:
        exit_reason = "verify_failed"
    else:
        exit_reason = "done"
    # `ok` é o veredito da régua, não a opinião do agente.
    ok = bool(decision and decision.action == "accept")

    row_id = store.record_run(
        RunRow(
            run_id=run_id,
            unit_id=unit.id,
            project=None,
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
        db,
    )
    store.record_node(run_id, "record", {"row_id": row_id}, db)
    return {"events": [_event("record", row_id=row_id, ok=ok)]}


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
    """Compila a topologia. Ela é immutable no genoma — o loop só calibra config."""
    from langgraph.graph import END, START, StateGraph

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
        events=[],
    )


def run_unit(
    unit_dir: Path,
    backend: str,
    model: str | None,
    data_dir: Path,
    thread_id: str,
    max_attempts: int = 2,
) -> RunState:
    """Roda uma unidade ponta a ponta. Mesmo `thread_id` + `data_dir` = retomada."""
    # Import tardio: o cli vai chamar o grafo, então o grafo não importa o cli
    # no topo (ciclo).
    from harness.cli import load_unit

    unit = load_unit(Path(unit_dir))
    data_dir = Path(data_dir)

    with open_checkpointer(data_dir) as checkpointer:
        graph = build_run_graph(checkpointer)
        config = {
            "configurable": {
                "thread_id": thread_id,
                CFG_DATA_DIR: str(data_dir),
                CFG_BACKEND: backend,
                CFG_MODEL: model,
                CFG_MAX_TURNS: DEFAULT_MAX_TURNS,
            },
            # Cada tentativa gasta ~8 supersteps; sobra folga para o loop de retry.
            "recursion_limit": 12 * (max_attempts + 1),
        }
        # `next` não vazio = thread parou no meio: retoma sem reinjetar entrada.
        pending = bool(graph.get_state(config).next)
        payload = None if pending else initial_state(unit, thread_id, max_attempts)
        return graph.invoke(payload, config)
