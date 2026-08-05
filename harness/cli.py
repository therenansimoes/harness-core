"""CLI do harness. `harness run` / `ab` / `backends` / `improve` / `replay` /
`lineage` / `export` / `import` / `doctor` / `skills` / `actions` / `seal` /
`frontier` / `evolve` / `ui-verify` / `vision-judge` / `bench` / `queue` /
`webhook`."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from harness import paths
from harness.ab import ArmSpec, run_ab
from harness.backends import registry
from harness.improve.counterfactual import DEFAULT_LIMIT as WHATIF_LIMIT
from harness.improve.counterfactual import MOCK_BACKEND
from harness.improve.replay import DEFAULT_LIMIT
from harness.ledger import store
from harness.projects import SETUP_TIMEOUT
from harness.report import DEFAULT_SINCE_HOURS as REPORT_SINCE
from harness.routing import MANUAL_TIER, ROUTE_AUTO, ROUTE_MANUAL, ROUTE_MODES, router
from harness.ruler.gate import Decision, gate
from harness.ruler.kpi import collect, load_kpis
from harness.ruler.verify import VERIFY_CHECK_NAME, log_tail, run_log_dir, run_verify
from harness.ruler.wilson import MIN_N, Arm, decide_ab, wilson_interval
from harness.types import Check, ExecRequest, ExecResult, RunRow, Selection, UnitSpec
from harness.uiverify import ASSET_KINDS, DEFAULT_MIN_KB, SHOT_NAME
from harness.workspace import cache_gc
from harness.workspace.provision import dispose, provision
from harness.workspace.sealing import is_verifier, verifier_visible

UNIT_FILE = "unit.toml"
SCRATCH_DIR = ".harness"  # log do verify; não conta como sujeira do repo-alvo
DEFAULT_MAX_TURNS = 30
HELD_IN = Path("benchmarks/held_in")  # unidades default do `harness improve`
WEBHOOK_PORT = 8787  # porta default do `harness webhook` (loopback)
# Resposta default do `--resume`: abortar. Retomar um loop sem dizer o que
# fazer não pode significar "continua sozinho" — quem foi chamado tem que
# escolher explicitamente continuar.
IMPROVE_ANSWER = '{"action":"abort"}'
# Nome de check: curto e sem espaço porque ele viaja em log, payload e hint.
CHECK_NAME_RE = re.compile(r"[a-z0-9_-]{1,32}")
PACKAGE = "harness-core"


def _versao() -> str:
    """Versão instalada, ou `(checkout)` rodando direto do repo sem instalar."""
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return "(checkout)"


def _bootstrap() -> None:
    """Telemetria de terceiro é opt-in explícito, nunca default."""
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")
    # langgraph-checkpoint 3.x: sem isto, DB comprometido executa código na
    # desserialização msgpack.
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")


def load_unit(path: Path) -> UnitSpec:
    """Carrega um `unit.toml` de um diretório (ou o próprio arquivo)."""
    unit_file = path / UNIT_FILE if path.is_dir() else path
    if not unit_file.is_file():
        raise FileNotFoundError(f"unit não encontrada: {unit_file}")
    data = tomllib.loads(unit_file.read_text(encoding="utf-8"))
    missing = [k for k in ("id", "prompt") if k not in data]
    project = data.get("project")
    verify_cmd = data.get("verify_cmd")
    if verify_cmd is None and project:
        # Unidade de projeto pode herdar o verify default do registro.
        from harness.projects import get_project

        verify_cmd = get_project(str(project)).verify_default
    if verify_cmd is None:
        missing.append("verify_cmd")
    if missing:
        raise ValueError(f"{unit_file}: campos faltando: {', '.join(missing)}")
    return UnitSpec(
        id=str(data["id"]),
        path=unit_file.parent,
        prompt=str(data["prompt"]),
        verify_cmd=str(verify_cmd),
        kind=data.get("kind"),
        project=str(project) if project else None,
        checks=_load_checks(unit_file, data.get("checks")),
        adapter=str(data["adapter"]) if data.get("adapter") else None,
        deps=_load_str_list(unit_file, "deps", data.get("deps")),
        files=_load_str_list(unit_file, "files", data.get("files")),
    )


def _load_str_list(unit_file: Path, field_name: str, raw: object) -> tuple[str, ...]:
    """`deps`/`files` do unit.toml -> tupla de string. Ausente = ().

    Item não-string é erro na hora de carregar: `deps = [1]` viraria o id "1",
    que não bate com unidade nenhuma, e a dependência sumiria em silêncio.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{unit_file}: {field_name} precisa ser lista de string")
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{unit_file}: {field_name} tem item não-string: {item!r}")
    return tuple(raw)


def _load_checks(unit_file: Path, raw: object) -> tuple[Check, ...]:
    """`[checks]` do unit.toml -> tupla de Check. Ausente = () = régua de sempre.

    Régua torta é erro na hora de carregar, não na hora de verificar: um check
    com peso zero ou comando não-determinístico entraria no score e mentiria
    sobre o quanto passou. `verify_cmd` é nome reservado — ele já é um check
    implícito de peso 1.0 dentro do score.
    """
    if raw is None:
        return ()
    from harness.add import AddError, validate_verify_cmd  # lazy: igual ao resto do cli

    if not isinstance(raw, dict):
        raise ValueError(f"{unit_file}: [checks] precisa ser tabela nome -> tabela")
    out: list[Check] = []
    for name, body in raw.items():
        if name == VERIFY_CHECK_NAME:
            raise ValueError(f"{unit_file}: [checks.{name}] é nome reservado")
        if not CHECK_NAME_RE.fullmatch(name):
            raise ValueError(
                f"{unit_file}: nome de check inválido: {name!r} (esperado [a-z0-9_-]{{1,32}})"
            )
        if any(c.name == name for c in out):
            raise ValueError(f"{unit_file}: check duplicado: {name}")
        if not isinstance(body, dict) or "cmd" not in body:
            raise ValueError(f"{unit_file}: [checks.{name}] precisa de cmd")
        try:
            weight = float(body.get("weight", 1.0))
        except (TypeError, ValueError):
            raise ValueError(f"{unit_file}: [checks.{name}] weight não é número") from None
        if not weight > 0 or math.isinf(weight):  # NaN já cai no `not > 0`
            raise ValueError(f"{unit_file}: [checks.{name}] weight precisa ser > 0")
        cmd = str(body["cmd"]).strip()
        try:
            validate_verify_cmd(cmd)  # mesma régua do `harness add`
        except AddError as exc:
            raise ValueError(f"{unit_file}: [checks.{name}] {exc}") from None
        out.append(Check(name=name, cmd=cmd, weight=weight))
    return tuple(out)


def seed_workspace(unit: UnitSpec, ws: Path) -> list[str]:
    """Copia os arquivos da unidade pro workspace (menos o próprio `unit.toml`
    e o verificador, que só aparece no instante do verify)."""
    copied: list[str] = []
    for src in sorted(unit.path.rglob("*")):
        rel = src.relative_to(unit.path)
        if rel.parts[0] == UNIT_FILE or "__pycache__" in rel.parts:
            continue
        if is_verifier(rel):
            continue
        dst = ws / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel.as_posix())
    return copied


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _dirty(repo: Path) -> list[str]:
    """Linhas de `git status --porcelain`, menos o scratch da própria régua."""
    proc = _git(repo, "status", "--porcelain")
    if proc.returncode != 0:
        raise ValueError(f"{repo} não é um repo git: {proc.stderr.strip()}")
    prefix = SCRATCH_DIR + "/"
    return [
        ln
        for ln in proc.stdout.splitlines()
        if ln.strip() and not ln[3:].strip('"').startswith(prefix)
    ]


def _revert(repo: Path) -> None:
    """Desfaz o que o run escreveu no repo-alvo (PR-3 troca por descartar o worktree).

    Revert que falha em silêncio é pior que não reverter: o ledger diria
    `revert` e o repo continuaria mudado. Por isso o erro do git vai pro stderr.
    """
    for cmd in (("checkout", "--", "."), ("clean", "-fdq")):
        proc = _git(repo, *cmd)
        if proc.returncode != 0:
            print(f"revert: git {' '.join(cmd)} falhou — {proc.stderr.strip()}", file=sys.stderr)


@contextlib.contextmanager
def _workspace(repo: str | None, run_id: str) -> Iterator[Path]:
    """Onde o backend trabalha: o repo-alvo (`--repo`) ou um tmpdir descartável.

    PR-3 troca isto por `workspace/provision.py` (git worktree). Até lá, `--repo`
    roda no próprio repo e por isso exige repo git limpo: `revert` é `git
    checkout -- . && git clean -fd`, e trabalho não commitado morreria junto.
    """
    if repo is None:
        with tempfile.TemporaryDirectory(prefix=f"harness-{run_id}-") as tmp:
            yield Path(tmp)
        return
    ws = Path(repo).expanduser().resolve()
    if not ws.is_dir():
        raise NotADirectoryError(f"--repo não é um diretório: {ws}")
    pending = _dirty(ws)
    if pending:
        raise ValueError(
            f"--repo {ws} tem mudança não commitada ({len(pending)} arquivo(s)); "
            "o run pode dar revert e levaria junto"
        )
    yield ws


def _exit_reason(result: ExecResult, decision: Decision) -> str:
    """Vocabulário do ledger: a decisão da régua manda, o executor complementa."""
    if decision.action == "accept":
        return "done"  # mesmo que o executor tenha estourado turnos
    if decision.reason.startswith("backend_"):
        return result.exit_reason  # nem chegou a executar (blocked/error)
    if decision.action == "retry":
        return "verify_failed"  # única outra causa de retry no gate
    return decision.reason  # revert carrega kpi_regression:… / tamper:…


class PreflightError(RuntimeError):
    """Backend indisponível: nada executou, então não há linha honesta pro ledger."""


@dataclass(frozen=True)
class RunOutcome:
    """O que um run produziu. A `Decision` viaja junto porque `RunRow` só guarda
    o `exit_reason`, que colapsa a ação da régua (`revert` e `retry` viram uma
    causa só) — quem imprime precisa dos dois."""

    row: RunRow
    decision: Decision
    # Fim do log do verify quando ele reprovou: o workspace onde o log mora já
    # foi descartado quando quem chamou lê isto.
    verify_tail: str = ""


def _record_episode(unit: UnitSpec, trace: str) -> None:
    """Verify vermelho vira caso na memória episódica (banco default: episódio é
    conhecimento entre runs, não do banco de um experimento).

    Mesmo fail-open do `_episodic_block` que consome isto no backend: import
    lazy e except largo — memória nunca derruba o run."""
    try:
        from harness.memory import episodic

        episodic.record_failure(unit.kind, unit.id, trace)
    except Exception:
        pass


def run_once(
    unit: UnitSpec,
    backend_name: str,
    model: str | None = None,
    *,
    repo: str | None = None,
    project: str | None = None,
    tier: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> RunOutcome:
    """Um run ponta a ponta: executa, verifica, mede KPI e passa pelo gate.

    Não grava no ledger — quem chama escolhe o banco (o A/B passa o `data_dir`
    do experimento) e o momento.
    """
    backend = registry.get_backend(backend_name)
    if model is not None and hasattr(backend, "model"):
        # Backend model-selectable checa o modelo pedido no próprio preflight.
        backend.model = model

    pre = backend.preflight()
    if not pre.ok:
        raise PreflightError(f"preflight falhou para {backend_name}: {pre.reason}")

    run_id = uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    with _workspace(repo, run_id) as ws:
        if repo is None:
            # tmpdir nasce vazio; no --repo os arquivos já são do repo-alvo.
            seed_workspace(unit, ws)
        sec_provision = time.monotonic() - t0
        # Specs do ANTES: a mudança avaliada não redefine a direção da régua.
        specs = load_kpis(ws)
        kpi_before = collect(ws, specs=specs)
        # Log de run anterior não fica no workspace para o agente ler: o golden
        # impresso pelo verificador selado seria a resposta pronta.
        (ws / ".harness" / "verify.log").unlink(missing_ok=True)
        result = backend.execute(
            ExecRequest(
                prompt=unit.prompt,
                workspace=ws,
                model=model,
                max_turns=max_turns,
                trace_path=ws / "trace.jsonl",
                run_id=run_id,
                kind=unit.kind,
            )
        )
        # A régua decide, não o executor: verify roda sempre que houve execução
        # (mesmo max_turns/timeout — pode ter consertado antes de estourar).
        # "blocked"/"error" nem chegaram a executar; aí não há o que verificar.
        # "stalled" fica fora de propósito: por definição é zero escrita no
        # workspace, então o verify só repetiria o resultado do run anterior.
        # "truncated" entra junto: a resposta foi cortada no teto de tokens, mas
        # o agente pode ter escrito o conserto antes — a régua tem o que julgar.
        # "blocker" entra pelo mesmo motivo do "truncated": o agente explicou por
        # que parou, mas pode ter escrito parte do conserto antes — a régua julga
        # o que foi escrito, não a explicação.
        ran = result.exit_reason in ("done", "max_turns", "timeout", "truncated", "blocker")
        verify_tail = ""
        if ran:
            # O verificador entra agora, com o agente já fora: durante o
            # execute ele não existe no workspace (prova selada).
            log_dir = run_log_dir(run_id)
            with verifier_visible(unit.path, ws):
                verdict = run_verify(unit, ws, log_dir=log_dir)
            if not verdict.passed:
                verify_tail = log_tail(verdict.log_path)
                _record_episode(unit, verify_tail)
            # `specs=` do ANTES: a mudança avaliada não pode redefinir a régua
            # reescrevendo o kpis.toml (buraco de Goodhart do review do PR-4).
            decision = gate(verdict, kpi_before, collect(ws, specs=specs), [], specs)
        else:
            decision = Decision("retry", f"backend_{result.exit_reason}")
        if decision.action == "revert" and repo is not None:
            _revert(ws)  # no tmpdir o revert é o próprio descarte

    sec_total = time.monotonic() - t0
    row = RunRow(
        run_id=run_id,
        unit_id=unit.id,
        project=project,
        backend=backend_name,
        model=model,
        tier=tier,
        kind=unit.kind,
        ok=decision.action == "accept",
        exit_reason=_exit_reason(result, decision),
        sec_total=sec_total,
        sec_provision=sec_provision,
        cost_usd=result.cost_usd,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        intervention=False,
        created_at=store.now_iso(),
    )
    return RunOutcome(row=row, decision=decision, verify_tail=verify_tail)


def _resolve_route(args: argparse.Namespace, unit: UnitSpec) -> Selection:
    """Quem executa esta unidade. `manual` obedece às flags; `auto` pergunta ao
    router, que lê o kind da unidade e o histórico do ledger.

    Combinação inválida sai como erro de argparse (uso + exit 2) e não como
    exceção no meio do run — mesma convenção do `ab --dim`.
    """
    if args.route == ROUTE_AUTO:
        if args.backend or args.model:
            args.parser.error("--route auto: quem escolhe backend/model é o router")
        sel = router.select(unit, history=store.history(project=args.project))
        # `max_turns` da flag ainda vence: o tier dá o default, não uma trava.
        turns = args.max_turns if args.max_turns is not None else sel.max_turns
        return replace(sel, max_turns=turns)

    if not args.backend:
        args.parser.error("--backend é obrigatório (ou use --route auto)")
    return Selection(
        backend=args.backend,
        model=args.model or "",
        # Vazio => `tier` NULL no ledger, como sempre foi nesta CLI: run no
        # dedo não é evidência de nenhuma classe de custo (o prior é keyed em
        # (kind, tier, backend)), e rotulá-lo agora mudaria o histórico.
        tier="",
        kind=unit.kind or "code",
        max_turns=args.max_turns if args.max_turns is not None else DEFAULT_MAX_TURNS,
        reasons=("manual:flag",),
    )


def _last_event(final: dict, node: str) -> dict:
    """Último evento de `node` no trace do grafo (vazio se o nó não rodou)."""
    return next((e for e in reversed(final.get("events", [])) if e.get("node") == node), {})


def _graph_final(args: argparse.Namespace, unit: UnitSpec, sel: Selection) -> tuple[dict, float]:
    """Roda a unidade no grafo e devolve `(estado final, segundos)`.

    Separado do `_run_via_graph` porque o `harness do` precisa do estado (branch
    de entrega, arquivos tocados, id do ledger) para montar o relatório dele; o
    `run` só precisa do código de saída.
    """
    from harness.graph.run_graph import run_unit

    # Flags que o grafo não recebe: dizer isto alto é melhor que obedecer pela
    # metade em silêncio.
    if args.max_turns is not None:
        print(
            "run: --max-turns não vale no modo projeto (o grafo usa o teto do "
            "config/graph.toml + governor)",
            file=sys.stderr,
        )
    if args.repo:
        print(
            "run: --repo ignorado no modo projeto (o workspace é o worktree do "
            f"repo registrado em project={unit.project!r})",
            file=sys.stderr,
        )

    # `auto` vai como auto: quem escolhe a cada tentativa é o router do grafo, e
    # é dele a escalação de tier no retry. `manual` leva a seleção já resolvida.
    auto = args.route == ROUTE_AUTO
    t0 = time.monotonic()
    final = run_unit(
        Path(args.unit),
        None if auto else sel.backend,
        None if auto else (sel.model or None),
        store.data_dir(),
        thread_id=uuid.uuid4().hex[:12],
        route=args.route,
    )
    return final, time.monotonic() - t0


def _run_via_graph(args: argparse.Namespace, unit: UnitSpec, sel: Selection) -> int:
    """`run` de unidade com `project=`: entrega em branch só existe no grafo.

    O worktree do repo (provision) e a poda no fim são nós do grafo; o fluxo
    inline do `run_once` roda em tmpdir e por isso nunca entregaria a branch.
    Rotear para cá é o que faz `harness run --unit` de projeto valer o mesmo que
    a fila. Saída equivalente à do fluxo inline: uma linha com a decisão e, no
    vermelho, o tail do verify no stderr (que o nó `verify` já gravou no ledger).
    """
    final, sec_total = _graph_final(args, unit, sel)

    decision = final.get("decision")
    if decision is None:
        print(f"run: grafo parou sem decisão (thread {final['run_id']})", file=sys.stderr)
        return 1
    graph_sel = final.get("selection")
    print(
        f"{final['run_id']} {unit.id} "
        f"{graph_sel.backend if graph_sel else '-'} "
        f"{decision.action} {decision.reason} {sec_total:.2f}s "
        f"ledger#{_last_event(final, 'record').get('row_id')}"
    )
    tail = _last_event(final, "verify").get("tail", "")
    if decision.action != "accept" and tail:
        print(f"verify falhou — últimas linhas do log:\n{tail}", file=sys.stderr)
    return 0 if decision.action == "accept" else 1


def cmd_run(args: argparse.Namespace) -> int:
    unit = load_unit(Path(args.unit))
    sel = _resolve_route(args, unit)
    if args.route == ROUTE_AUTO:
        print(
            f"route auto {unit.id} kind={sel.kind} tier={sel.tier} "
            f"{sel.backend} {sel.model or '-'} [{' '.join(sel.reasons)}]"
        )
    if unit.project:
        return _run_via_graph(args, unit, sel)
    try:
        outcome = run_once(
            unit,
            sel.backend,
            sel.model or None,
            repo=args.repo,
            project=args.project,
            tier=sel.tier or None,
            max_turns=sel.max_turns,
        )
    except PreflightError as exc:
        print(exc, file=sys.stderr)
        return 1

    row, decision = outcome.row, outcome.decision
    row_id = store.record_run(row)
    if outcome.verify_tail:
        # `run` avulso não passa pelo grafo, então o node_event do verify é o
        # único lugar onde o porquê da falha sobrevive ao workspace.
        store.record_node(
            row.run_id,
            "verify",
            {
                "passed": False,
                "exit_reason": row.exit_reason,
                "tail": outcome.verify_tail,
            },
        )
    print(
        f"{row.run_id} {unit.id} {row.backend} {decision.action} {decision.reason} "
        f"{row.sec_total:.2f}s ledger#{row_id}"
    )
    if not row.ok and outcome.verify_tail:
        print(
            f"verify falhou — últimas linhas do log:\n{outcome.verify_tail}",
            file=sys.stderr,
        )
    return 0 if row.ok else 1


# --------------------------------------------------------------------------- do


def _linha_do(rotulo: str, valor: object) -> str:
    """Rótulo alinhado à direita na largura de `resultado`, que é o mais longo."""
    return f"{rotulo:>9}  {valor}"


def _rota_txt(modo: str, sel: Selection) -> str:
    return f"{modo} → {sel.tier or MANUAL_TIER} {sel.backend} {sel.model or '-'} (kind={sel.kind})"


def _sem_turno(final: dict) -> bool:
    """Nenhuma tentativa chegou a falar com um modelo — preflight barrou t0.

    É o sintoma de servidor local desligado, e é diferente de "o agente tentou e
    errou": não há o que reflexionar nem por que gastar retry no mesmo tier.
    """
    execs = [e for e in final.get("events", []) if e.get("node") == "execute"]
    if not execs:
        return True
    if not all(e.get("exit_reason") == "blocked" for e in execs):
        return False
    res = final.get("exec")
    return res is None or res.turns == 0


def _proximo_tier(sel: Selection):
    """Tier imediatamente mais caro que o da seleção, ou None se já é o topo (ou
    se a seleção nem veio da tabela de custo)."""
    try:
        cfg = router.load_config()
        atual = router.tier_by_name(cfg, sel.tier)
    except (router.RouterError, OSError):
        return None
    acima = router.tier_by_rank(cfg, atual.cost_rank + 1)
    return None if acima.name == atual.name else acima


def _motivo_nao_aceito(decision) -> str:
    """Tradução do vocabulário do gate para quem não conhece o vocabulário."""
    reason = decision.reason if decision else ""
    if not decision:
        return "o grafo parou sem decisão"
    if reason.startswith("verify_failed"):
        return "verify vermelho"
    if reason.startswith("kpi_regression"):
        return f"regressão de KPI ({reason.split(':', 1)[-1]})"
    return reason or decision.action


def cmd_do(args: argparse.Namespace) -> int:
    """`harness do "<pedido>"`: o comando de quem não conhece o resto da CLI.

    Faz na ordem o que o usuário teria de fazer na mão — `init`, escrever a
    unidade, escolher a régua, `run --route auto`, `integrate` — e imprime um
    bloco legível em vez do vocabulário interno. Nenhum passo é novo: o que este
    comando adiciona é a decisão de cada default.
    """
    from harness import do as do_mod
    from harness.add import AddError, validate_verify_cmd
    from harness.projects import IntegrateError, default_branch, delivery_branch, integrate

    paths.ensure_user_config()
    do_mod.pin_home_paths()
    repo, criado = do_mod.ensure_repo(Path.cwd())
    if criado:
        print(f"repo git criado em {repo} (commit inicial com o que já estava lá)")

    if args.verify_cmd:
        try:
            validate_verify_cmd(args.verify_cmd)
        except AddError as exc:
            print(exc, file=sys.stderr)
            return 2
        verify_cmd, motivo = args.verify_cmd, "você escolheu"
    else:
        verify_cmd, detectado = do_mod.detect_verify(repo)
        motivo = f"detectado: {detectado}"

    project = do_mod.ensure_project(repo)
    unit_dir = do_mod.write_unit(
        repo,
        do_mod.new_unit_id(args.task),
        args.task,
        verify_cmd,
        project,
        kind=args.kind,
    )

    args.unit = str(unit_dir)
    # Sem `--route`, quem manda é a presença de `--backend`/`--model`: pedir um
    # executor no dedo E deixar o router escolher seria contradição, e o erro do
    # `_resolve_route` não é vocabulário de quem chegou pelo `do`.
    if args.route is None:
        args.route = ROUTE_MANUAL if (args.backend or args.model) else ROUTE_AUTO
    unit = load_unit(unit_dir)
    sel = _resolve_route(args, unit)

    print(f"harness do · {repo} ({do_mod.current_branch(repo)})")
    print(_linha_do("pedido", args.task))
    print(_linha_do("régua", f"{verify_cmd}  ({motivo})"))
    if args.dry_run:
        print(_linha_do("rota", _rota_txt(args.route, sel)))
        print(_linha_do("unit", unit_dir))
        return 0

    try:
        final, sec = _graph_final(args, unit, sel)
        # Servidor local desligado não é fracasso da unidade: sobe um degrau de
        # custo e tenta de novo, UMA vez. Teto de duas execuções por comando —
        # subir tier em cascata é decisão de orçamento, não de disponibilidade.
        if _sem_turno(final) and (acima := _proximo_tier(final.get("selection") or sel)):
            print(f"LM Studio fora do ar (porta 1234) — subindo para {acima.name}")
            args.route, args.backend, args.model = ROUTE_MANUAL, acima.backend, acima.model or None
            sel = _resolve_route(args, unit)
            final, sec = _graph_final(args, unit, sel)
        elif _sem_turno(final):
            print(
                "nenhum executor respondeu e não há tier acima deste — rode `harness doctor`",
                file=sys.stderr,
            )
            return 2

        decision = final.get("decision")
        aceito = decision is not None and decision.action == "accept"
        res = final.get("exec")
        print(_linha_do("rota", _rota_txt(args.route, final.get("selection") or sel)))
        print(_linha_do("plano", "sim" if _last_event(final, "plan").get("needs_plan") else "não"))

        aplicado = ""
        if aceito and not args.no_apply:
            branch = _last_event(final, "accept").get("branch") or delivery_branch(unit.id)
            alvo = default_branch(repo)
            try:
                integrate(project, unit.id)
                aplicado = f" · aplicado em {alvo} ({branch})"
            except IntegrateError as exc:
                print(exc, file=sys.stderr)
                aplicado = f" · NÃO aplicado — está em {branch}, junte com: git merge {branch}"
        elif aceito:
            aplicado = f" · --no-apply: ficou em {delivery_branch(unit.id)}"

        if aceito:
            arquivos = len(res.files_changed) if res else 0
            print(_linha_do("resultado", f"ACEITO em {sec:.1f}s · {arquivos} arquivo(s){aplicado}"))
        else:
            print(_linha_do("resultado", f"NÃO ACEITO ({_motivo_nao_aceito(decision)})"))
        print(
            _linha_do(
                "ledger",
                f"#{_last_event(final, 'record').get('row_id')} · run {final['run_id']} "
                f"· detalhes: harness report",
            )
        )
        if not aceito:
            tail = _last_event(final, "verify").get("tail", "")
            if tail:
                print(f"verify falhou — últimas linhas do log:\n{tail}", file=sys.stderr)
            print("nada foi aplicado no seu repo", file=sys.stderr)
        return 0 if aceito else 1
    finally:
        if not args.keep_unit:
            shutil.rmtree(unit_dir, ignore_errors=True)
            with contextlib.suppress(OSError):
                unit_dir.parent.rmdir()  # `.harness/units` vazio não fica de lembrança


def cmd_quickstart(args: argparse.Namespace) -> int:
    """Primeiros 30 segundos: semeia `~/.harness`, ensina o caminho e diz o que falta.

    Roda o doctor inline porque a pergunta que vem logo depois de instalar é
    sempre a mesma ("por que não rodou?"), e a resposta quase sempre é uma peça
    de fora do harness (servidor local desligado, CLI não instalado). Só o que
    NÃO está ok aparece — lista de 25 linhas verdes não é boas-vindas.
    """
    from harness import doctor

    paths.ensure_user_config()
    print(f"harness {_versao()} · config em {paths.home_root()}")
    print()
    print("1. instalar     uv tool install harness-core")
    print("2. entrar       cd no seu projeto")
    print('3. pedir        harness do "conserta o bug em target.py"')
    print()

    # Com `root=` explícito: o quickstart roda de qualquer cwd, e o ambiente que
    # ele acabou de semear é `~/.harness`.
    pendencias = [c for c in doctor.checks(root=paths.home_root()) if c.status != doctor.OK]
    if not shutil.which("git"):
        pendencias.append(doctor.Check("git", doctor.FAIL, "git não está no PATH — instale antes"))
    if not pendencias:
        print("ambiente pronto: nada faltando.")
        return 0
    print("o que falta (nada aqui impede tentar):")
    for c in pendencias:
        print(f"  {c.status:<5} {c.name:<20} {c.detail}")
    return 0


def _arm(text: str) -> Arm:
    """`sucessos/tentativas` -> Arm. É o formato de `harness ab --a 5/6`."""
    succ_raw, sep, n_raw = text.partition("/")
    if not sep:
        raise argparse.ArgumentTypeError(f"esperado sucessos/tentativas (ex.: 5/6), veio {text!r}")
    try:
        succ, n = int(succ_raw), int(n_raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"sucessos e tentativas têm que ser inteiros: {text!r}"
        ) from None
    if n <= 0 or succ < 0 or succ > n:
        raise argparse.ArgumentTypeError(
            f"exigido 0 <= sucessos <= tentativas e tentativas > 0: {text!r}"
        )
    return Arm(succ=succ, n=n)


def _fmt_arm(arm: Arm) -> str:
    lo, hi = wilson_interval(arm.succ, arm.n)
    return f"{arm.succ}/{arm.n} [{lo:.2f},{hi:.2f}]"


def _print_ab_run(label: str, i: int, row: RunRow) -> None:
    """Uma linha curta por run: o A/B com backend de verdade demora, e sem isto
    a CLI fica muda até o fim."""
    model = "" if row.model is None else f":{row.model}"
    status = "ok" if row.ok else "falhou"
    print(f"{label}{i} {row.backend}{model} {status} {row.exit_reason} {row.sec_total:.2f}s")


def cmd_ab(args: argparse.Namespace) -> int:
    """Veredito de Wilson de B (candidata) contra A (baseline).

    Dois modos, mutuamente exclusivos: sem `--dim` os braços vêm prontos
    (`--a 5/6`), com `--dim` o harness roda o experimento e conta os sucessos.

    Sai 0 em qualquer veredito: DISCARD e INCONCLUSIVE são respostas da régua,
    não erro da CLI. Quem decide o que fazer com o veredito é o chamador.
    """
    if args.dim is not None:
        return _cmd_ab_run(args)
    if args.a is None or args.b is None:
        args.parser.error("modo estatístico exige --a e --b (ou use --dim)")
    verdict = decide_ab(args.a, args.b, min_n=args.min_n)
    print(f"{verdict} a={_fmt_arm(args.a)} b={_fmt_arm(args.b)}")
    return 0


def _cmd_ab_run(args: argparse.Namespace) -> int:
    """`--dim backend`: mesma unidade nos dois executores, n vezes cada."""
    if args.a is not None or args.b is not None:
        args.parser.error("--dim roda o experimento; --a/--b já trazem o resultado pronto")
    missing = [
        "--" + f.replace("_", "-")
        for f in ("unit", "a_backend", "b_backend")
        if getattr(args, f) is None
    ]
    if missing:
        args.parser.error(f"--dim {args.dim} exige {', '.join(missing)}")
    if args.n <= 0:
        args.parser.error(f"--n tem que ser positivo: {args.n}")

    try:
        report = run_ab(
            Path(args.unit),
            ArmSpec(backend=args.a_backend, model=args.a_model),
            ArmSpec(backend=args.b_backend, model=args.b_model),
            n=args.n,
            min_n=args.min_n,
            project=args.project,
            on_run=_print_ab_run,
        )
    except PreflightError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"{report.verdict} a={_fmt_arm(report.arm_a)} b={_fmt_arm(report.arm_b)}")
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    for name in registry.available():
        try:
            pre = registry.get_backend(name).preflight()
            status = "ok" if pre.ok else "indisponível"
            print(f"{name:<16} {status:<14} {pre.reason}")
        except Exception as exc:  # backend quebrado não derruba a listagem
            print(f"{name:<16} {'erro':<14} {exc}")
    return 0


def _improve_units(args: argparse.Namespace) -> list[Path]:
    """Unidades de avaliação: as pedidas, ou o held_in inteiro.

    Sem unidade não há experimento — e falhar dizendo isso é melhor que rodar
    um A/B sobre zero evidência e imprimir INCONCLUSIVE com cara de resposta.
    """
    if args.unit:
        return [Path(u) for u in args.unit]
    found = sorted(p.parent for p in HELD_IN.glob(f"*/{UNIT_FILE}"))
    if not found:
        raise FileNotFoundError(
            f"nenhum --unit e nenhuma unidade em {HELD_IN}/*/{UNIT_FILE}: "
            "o loop precisa de unidade de avaliação para medir a mutação"
        )
    return found


def _pending_escalation(thread_id: str) -> dict:
    """Payload da escalação que ainda espera resposta nesta thread, ou {}.

    Lê o checkpoint direto porque o `run_autopilot` só devolve escalação
    PENDENTE — respondida, ela sai do estado no próprio nó `escalate`, e o
    relatório do resume não tem mais como dizer a que pergunta o humano
    respondeu. Fail-open: memória de casos não derruba um resume.
    """
    try:
        from harness.graph.autopilot_graph import build_autopilot_graph
        from harness.graph.checkpoint import open_checkpointer

        with open_checkpointer(store.data_dir()) as checkpointer:
            graph = build_autopilot_graph(checkpointer)
            state = graph.get_state({"configurable": {"thread_id": thread_id}})
        return dict((state.values or {}).get("escalation") or {})
    except Exception:
        return {}


def _decision_terms(kind, reason, evidence: dict) -> str:
    """Cabeçalho de termos ESTÁVEIS do contexto gravado — o índice do caso.

    É por estes termos que a tentativa seguinte acha este caso: `kind`, motivo,
    `check:<nome>` de cada check reprovado e a classe de saída. Mesma lista que
    `run_graph._prior_query` monta na leitura; se um lado mudar sem o outro, o
    recall volta a ser vazio silencioso.

    A evidência é chave livre (cada nó põe a sua), então os nomes de check são
    procurados nas grafias que os nós usam hoje e nada mais — chave ausente
    simplesmente não contribui termo.
    """
    terms = [str(kind or ""), str(reason or "")]
    for key in ("failed", "failed_checks", "checks_failed"):
        raw = evidence.get(key)
        if isinstance(raw, str):
            raw = [raw]
        for name in raw or ():
            if name:
                terms.append(f"check:{name}")
    terms.append(str(evidence.get("exit_reason") or ""))
    return " ".join(t for t in terms if t)


def _record_human_decision(pending: dict, answer: dict | None) -> None:
    """Grava na memória de casos o par (escalação, resposta do humano).

    `context` é a evidência SEM o `prior_decisions`: o bloco de precedentes já é
    memória renderizada, e regravá-lo faria cada resposta carregar a anterior
    inteira — em três escalações o caso viraria só histórico do histórico.

    Na frente da evidência vai o cabeçalho de `_decision_terms`: o JSON sozinho
    guarda o caso mas não o torna encontrável, porque quem procura na tentativa
    seguinte não tem o texto da evidência — tem só kind, check reprovado e classe
    de saída.
    """
    from harness.memory import decisions

    evidence = dict(pending.get("evidence") or {})
    evidence.pop("prior_decisions", None)
    kind = evidence.pop("kind", None)
    reason = pending.get("reason")
    body = json.dumps(evidence, sort_keys=True, default=str, ensure_ascii=False)
    terms = _decision_terms(kind, reason, evidence)
    decisions.record_decision(
        kind,
        reason,
        f"{terms} {body}" if terms else body,
        json.dumps(answer, sort_keys=True, default=str, ensure_ascii=False),
    )


def cmd_improve(args: argparse.Namespace) -> int:
    """Um ciclo (ou mais) do loop de melhoria: propor, testar em A/B, decidir.

    Sai 0 em qualquer veredito, inclusive escalação: "preciso de humano" é
    resposta do loop, não erro da CLI — mesma regra do `harness ab`.

    `--resume <thread_id>` responde a escalação pendente daquela thread com o
    `--answer` (JSON) e deixa o grafo seguir de onde parou.
    """
    from harness.graph.autopilot_graph import run_autopilot
    from harness.improve.target import CatalogError

    answer = None
    pending = {}
    if args.resume:
        raw = args.answer if args.answer is not None else IMPROVE_ANSWER
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError as exc:
            args.parser.error(f"--answer não é JSON: {exc}")
        if not isinstance(answer, dict):
            args.parser.error(f"--answer tem que ser um objeto JSON: {raw!r}")
        # A pergunta tem que ser lida ANTES do resume: o nó `escalate` limpa
        # `escalation` do estado ao ser respondido, e sem os dois lados juntos
        # (motivo + evidência de lá, resposta de cá) não há caso para gravar.
        pending = _pending_escalation(args.resume)
    elif args.answer is not None:
        # Sentinela None, e não comparação com o texto do default: com
        # `!= IMPROVE_ANSWER` quem digitasse exatamente o JSON default sem
        # `--resume` tinha a flag ignorada em silêncio — e flag ignorada em
        # silêncio é a que faz o humano achar que respondeu à escalação.
        args.parser.error("--answer só faz sentido com --resume")

    try:
        units = _improve_units(args)
        report = run_autopilot(
            store.data_dir(),
            cycles=args.cycles,
            deadline_s=args.deadline_s,
            units=units,
            backend=args.backend,
            model=args.model,
            n=args.n,
            thread_id=args.resume,
            resume=answer,
        )
    except (FileNotFoundError, ValueError, CatalogError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if pending:
        _record_human_decision(pending, answer)

    for r in report.results:
        change = f" {r['key']} {r['change']}" if r.get("key") else ""
        delta = "" if r.get("delta") is None else f" delta={r['delta']:+.2f}"
        print(
            f"ciclo{r['cycle']} {r['rule_id']}{change} {r['verdict']} "
            f"a={r['arm_a']} b={r['arm_b']}{delta} "
            f"{'revertida' if r['reverted'] else 'mantida'} mut={r['mutation_id']}"
            + (f" ({r['note']})" if r.get("note") else "")
        )
    if report.escalation:
        e = report.escalation
        # `thread` na linha porque é o que o humano precisa digitar de volta:
        # `harness improve --resume <thread> --answer '{...}'`.
        print(
            f"escalate {e['reason']} thread={report.thread_id} evidence={e['evidence']}",
            file=sys.stderr,
        )
    print(
        f"improve ciclos={report.cycles} mutações={len(report.results)} "
        f"intervenções={report.interventions} "
        f"intervention_rate={report.intervention_rate:.2f} (n={report.runs_window})"
    )
    return 0


def _fmt_window(succ: int, n: int, ci: tuple[float, float]) -> str:
    return f"{succ}/{n} [{ci[0]:.2f},{ci[1]:.2f}]"


def _attribution_lines(att) -> list[str]:
    """Três linhas: o que era a mutação, as duas janelas, o que confunde.

    A terceira sai SEMPRE, mesmo com zero: "confounders=0" é informação (a
    janela está limpa), e omitir a linha faria o leitor achar que ninguém olhou.
    """
    chave = " ".join(
        f"{name}={value}"
        for name, value in zip(("kind", "tier", "backend"), att.key, strict=False)
        if value
    )
    delta = "n/a" if att.delta is None else f"{att.delta:+.2f}"
    leitura = (
        "sem amostra" if att.delta is None else "separados" if att.separated else "sobrepostos"
    )
    nomes = " ".join(f"{c.mutation_id}:{c.rule_id}@{c.applied_at}" for c in att.confounders)
    return [
        f"mut {att.mutation_id} {att.rule_id} {att.verdict} "
        f"{'revertida' if att.reverted else 'mantida'} "
        f"exp={att.n_experiment} {chave or 'chave=-'}",
        f"antes {_fmt_window(att.succ_before, att.n_before, att.ci_before)} "
        f"depois {_fmt_window(att.succ_after, att.n_after, att.ci_after)} "
        f"delta={delta} intervalos={leitura}",
        f"confounders={len(att.confounders)}" + (f" {nomes}" if nomes else ""),
    ]


def cmd_replay(args: argparse.Namespace) -> int:
    """Atribuição por mutação: quanto do delta o histórico sustenta.

    Sai 0 com qualquer número, inclusive delta n/a: "não tenho amostra depois"
    é resposta honesta do replay, não erro da CLI. Só mutação inexistente sai 1.

    `--limit` vale nos dois modos, com o MESMO número: o `--list` é como se
    descobre o id, e listar com um teto e atribuir com outro faz o `--list`
    mostrar id que o `--mutation` jura não existir.
    """
    from harness.improve.replay import ReplayError, replay

    if args.list:
        rows = store.mutations(limit=args.limit)
        for m in rows:
            print(
                f"{m.mutation_id} {m.applied_at} {m.rule_id} {m.verdict} "
                f"a={m.arm_a} b={m.arm_b} "
                f"{'revertida' if m.reverted else 'mantida'}" + (f" ({m.note})" if m.note else "")
            )
        # No teto, `mutações=N` seria lido como "o ledger tem N": dizer que a
        # lista bateu no limite é a diferença entre truncar e mentir.
        teto = " (teto do --limit; pode haver mais)" if len(rows) == args.limit else ""
        print(f"mutações={len(rows)}{teto}")
        return 0

    if not args.mutation:
        args.parser.error("informe --mutation <id> (ou --list para ver os ids)")
    try:
        att = replay(args.mutation, limit=args.limit)
    except ReplayError as exc:
        print(exc, file=sys.stderr)
        return 1
    for line in _attribution_lines(att):
        print(line)
    return 0


def cmd_whatif(args: argparse.Namespace) -> int:
    """Replay contrafactual: a config de hoje teria salvado os fracassos?

    Sai 0 sempre que o relatório sai, inclusive com zero salvos e inclusive com
    o ledger sem fracasso nenhum: "a config de hoje não salva nada disso" é
    resultado da medição, não erro da CLI.
    """
    from harness.improve.counterfactual import run_whatif

    run_whatif(kind=args.kind, limit=args.limit, backend=args.backend, model=args.model)
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    """Árvore genealógica das mutações de código, com veredito do ledger.

    `--limit N` corta pelas N ÚLTIMAS raízes (as mais recentes), não pelas
    primeiras: quem olha linhagem quer o presente da evolução, não a origem.
    """
    from harness.improve import lineage

    entries = lineage.load_lineage(args.file)
    if not entries:
        print("sem linhagem ainda — nenhuma mutação de código registrada.")
        return 0
    lineage.enrich(entries, db_path=args.db)
    tree = lineage.build_tree(entries)
    if args.limit is not None:
        tree = tree[-args.limit :]
    print(lineage.render(tree))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Empacota skills + prior de roteamento para o próximo projeto."""
    from harness.transfer import export_bundle

    out = export_bundle(args.out)
    print(f"bundle {out}")
    return 0


def cmd_import_bundle(args: argparse.Namespace) -> int:
    """Traz o bundle de outro projeto. Colisão nunca sobrescreve — só reporta."""
    from harness.transfer import import_bundle

    summary = import_bundle(args.bundle)
    for name in summary["imported"]:
        print(f"importada {name}")
    for name, reason in summary["skipped"]:
        print(f"pulada    {name} ({reason})")
    print(
        f"importadas={len(summary['imported'])} "
        f"puladas={len(summary['skipped'])} "
        f"ações no prior={summary['prior_actions']}"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnóstico local. Exit 1 só com FALHA: aviso é o mundo, não o harness."""
    from harness import doctor

    result = doctor.checks()
    for c in result:
        print(f"{c.status:<5} {c.name:<20} {c.detail}")
    bad = doctor.failures(result)
    avisos = sum(1 for c in result if c.status == doctor.WARN)
    print(f"doctor checks={len(result)} falhas={len(bad)} avisos={avisos}")
    return 1 if bad else 0


def cmd_skills(args: argparse.Namespace) -> int:
    """Lista as skills carregáveis; `--lift` anexa a atribuição do ledger.

    Lift sem amostra num dos braços sai como traço: número inventado sobre
    zero trial é pior que admitir que ninguém mediu ainda.
    """
    from harness.improve import root_dir
    from harness.skills.loader import load_skills

    skills = load_skills(root_dir() / "skills")
    for s in skills:
        kinds = ",".join(s.kinds) or "*"
        line = f"{s.name:<24} {kinds:<12} {s.description}"
        if args.lift:
            from harness.skills.attribution import lift

            d = lift(s.name)
            (w_s, w_t), (wo_s, wo_t) = d["with"], d["without"]
            delta = (
                "-"
                if w_t == 0 or wo_t == 0
                else f"{d['wilson_low_with'] - d['wilson_low_without']:+.2f}"
            )
            line += f" com={w_s}/{w_t} sem={wo_s}/{wo_t} lift={delta}"
        print(line)
    print(f"skills={len(skills)}")
    return 0


def cmd_cache_gc(args: argparse.Namespace) -> int:
    """Poda o cache compartilhado de dependência até caber no teto.

    LRU com trava: o que foi tocado nas últimas 24h fica, mesmo estourando o
    teto — pode ser cache de run em voo. `--dry-run` só mostra o uso.
    """
    usado, arquivos = cache_gc.usage()
    teto = int(args.max_gb * cache_gc.GB)
    if args.dry_run:
        estado = "acima do teto" if usado > teto else "ok"
        print(
            f"cache {cache_gc.human(usado)} em {arquivos} arquivo(s) "
            f"(teto {cache_gc.human(teto)}): {estado}"
        )
        return 0
    r = cache_gc.gc(max_gb=args.max_gb)
    print(
        f"cache {cache_gc.human(r['before'])} -> {cache_gc.human(r['after'])} "
        f"(teto {cache_gc.human(r['max_bytes'])}): {r['removed']} arquivo(s) removido(s), "
        f"{cache_gc.human(r['freed'])} liberado(s), {r['skipped_recent']} preservado(s) "
        "por uso recente (<24h)"
    )
    if r["after"] > r["max_bytes"]:
        # Não é falha: o que sobrou é cache quente, e derrubar run vivo é pior.
        print("ainda acima do teto — o restante é uso recente", file=sys.stderr)
    return 0


def cmd_procs(args: argparse.Namespace) -> int:
    """Lista servidores registrados nos workspaces; `--reap` mata os órfãos.

    Órfão é o registro cujo `harness_pid` já morreu: o run que subiu o servidor
    não existe mais, então ninguém vai chamar o cleanup dele. Registro de run
    VIVO nunca é tocado — matar servidor de run em andamento é sabotagem.
    """
    from harness.backends import procs
    from harness.workspace.provision import ws_root

    total = mortos = orfaos = 0
    for path in sorted(ws_root().glob(f"*/{procs.HARNESS_SUBDIR}/{procs.PROCS_FILE}")):
        ws = path.parent.parent
        for entry in procs.read_procs(ws):
            total += 1
            orfao = not _pid_vivo(entry.get("harness_pid"))
            marca = "órfão" if orfao else "ativo"
            print(
                f"{entry.get('id', '?'):<10} {marca:<6} pid={entry.get('pid')} "
                f"porta={entry.get('port')} run={entry.get('run_id')} "
                f"cmd={entry.get('command')}"
            )
            if orfao:
                orfaos += 1
                if args.reap:
                    mortos += procs.stop(ws, str(entry.get("id")))
    print(f"procs={total} órfãos={orfaos}" + (f" mortos={mortos}" if args.reap else ""))
    return 0


def _pid_vivo(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)  # type: ignore[arg-type]
    except (OSError, TypeError, ValueError):
        return False
    return True


def cmd_actions(args: argparse.Namespace) -> int:
    """Lista as ações do registry e, havendo mutações, o placar KEEP/DISCARD."""
    from harness.improve.policy import action_of
    from harness.improve.target import actions

    acts = actions()
    muts = store.mutations(limit=None)
    # Placar por ação: a coluna `action` do ledger, com fallback pro token do
    # note nas linhas antigas (`action_of`). Ação sem linha aparece zerada — é
    # a que o bandit ainda vai explorar.
    tally: dict[str, list[int]] = {name: [0, 0] for name in acts}
    for m in muts:
        name = action_of(m)
        if name is None:
            continue
        t = tally.setdefault(name, [0, 0])
        if m.verdict == "KEEP":
            t[0] += 1
        elif m.verdict == "DISCARD":
            t[1] += 1
    for name in sorted(tally):
        keep, discard = tally[name]
        print(f"{name} KEEP={keep} DISCARD={discard}")
    if muts:
        keep = sum(1 for m in muts if m.verdict == "KEEP")
        discard = sum(1 for m in muts if m.verdict == "DISCARD")
        print(f"ações={len(acts)} mutações={len(muts)} KEEP={keep} DISCARD={discard}")
    else:
        print(f"ações={len(acts)} sem mutações no ledger")
    return 0


def cmd_ui_verify(args: argparse.Namespace) -> int:
    """`harness ui-verify`: a régua abre a página e olha a tela.

    Sem severidade dupla, ao contrário do `doctor`: aqui não existe "o mundo não
    estava pronto". Quem põe isto no `verify_cmd` já decidiu que a tela faz parte
    do aceite, então qualquer falha derruba o exit code — inclusive Chrome
    ausente, que significa "não foi verificado", não "passou".

    Exceção deliberada: `<a href>` local morto sai como AVISO e não derruba o
    exit code, porque numa fila progressiva o nav aponta para páginas que a
    unidade seguinte ainda vai criar. `--strict-links` devolve o gate completo.
    """
    from harness import uiverify

    res = uiverify.verify(
        args.dist,
        url_path=args.url_path,
        min_kb=args.min_kb,
        expect=tuple(args.expect_asset or ()),
        shot_out=args.shot_out,
        ask=args.ask,
        strict_links=args.strict_links,
    )
    for motivo in res.failures:
        print(f"ui-verify FALHA {motivo}", file=sys.stderr)
    for aviso in res.warnings:
        print(f"ui-verify aviso: {aviso}", file=sys.stderr)
    shot = f"{res.shot} ({res.shot_kb:.1f}kb)" if res.shot else "nenhum"
    print(
        f"ui-verify dist={args.dist} url={args.url_path} "
        f"assets={res.ok_assets}/{res.checked} shot={shot} falhas={len(res.failures)}"
        + (f" avisos={len(res.warnings)}" if res.warnings else "")
    )
    return 1 if res.failures else 0


def cmd_vision_judge(args: argparse.Namespace) -> int:
    """`harness vision-judge`: um VLM local olha a tela e dá nota.

    Subcheck da régua GRADUADA, não régua binária: entra no `[checks]` do
    unit.toml com peso pequeno. A diferença em relação ao `ui-verify` está toda
    no fail-open — juiz probabilístico não reprova quando falta o juiz. Sem
    `[vision]` no models.toml, servidor mudo ou resposta ilegível saem 0 com o
    motivo em stderr. O que reprova aqui é o que o modelo VIU e achou ruim.

    `--min-nota baseline` troca o piso absoluto pelo relativo: a nota aceita da
    última vez. Sem baseline gravado, passa e grava — o primeiro run é que define
    o chão.
    """
    from harness import quality_baseline, vision
    from harness.backends.dom_tools import VAZIA, render

    ws = Path(args.ws or ".")
    piso, usa_baseline = _min_nota(args, ws)

    shot, kb, erro = render(ws, port=args.port, dist_path=args.dist)
    if erro:
        # Tela vazia é falha REAL da unidade; Chrome ausente é "não foi
        # verificado" e não pode reprovar um check de peso opcional.
        vazia = VAZIA in erro
        print(f"vision-judge {'FALHA' if vazia else 'aviso'} {erro}", file=sys.stderr)
        print(f"vision-judge shot=nenhum {'reprovado' if vazia else 'fail-open'}")
        return 1 if vazia else 0

    assert shot is not None
    if args.ref:
        res = vision.compare_reference(shot, Path(args.ref), question=args.question)
        if res["unavailable"]:
            print(f"vision-judge aviso: {res['unavailable']}", file=sys.stderr)
            print(f"vision-judge shot={shot} ({kb:.1f}kb) fail-open")
            return 0
        melhor = res["melhor"]
        if melhor != "a":
            print(f"vision-judge FALHA a referência está melhor: {res['motivo']}", file=sys.stderr)
        print(f"vision-judge shot={shot} ({kb:.1f}kb) ref={args.ref} melhor={melhor}")
        return 0 if melhor == "a" else 1

    res = vision.judge_image(shot, question=args.question)
    if res["unavailable"]:
        print(f"vision-judge aviso: {res['unavailable']}", file=sys.stderr)
        print(f"vision-judge shot={shot} ({kb:.1f}kb) fail-open")
        return 0
    nota = float(res["nota"])
    for bullet in res["bullets"]:
        print(f"vision-judge - {bullet}", file=sys.stderr)
    passou = piso is None or nota >= piso
    if usa_baseline and passou:
        # Sobe o chão só quando a nota foi aceita: baseline que grava nota pior
        # deixaria o gate afrouxar sozinho.
        quality_baseline.save_baseline(ws, {"nota": nota})
    if not passou:
        print(f"vision-judge FALHA nota {nota:.1f} < {piso:.1f}", file=sys.stderr)
    print(
        f"vision-judge shot={shot} ({kb:.1f}kb) nota={nota:.1f} "
        f"piso={'nenhum' if piso is None else f'{piso:.1f}'}"
        + (" (baseline)" if usa_baseline else "")
    )
    return 0 if passou else 1


def _min_nota(args: argparse.Namespace, ws: Path) -> tuple[float | None, bool]:
    """(piso, é_baseline). `baseline` sem arquivo = sem piso: passa e grava."""
    from harness import quality_baseline

    bruto = str(args.min_nota)
    if bruto.strip().lower() == "baseline":
        anterior = (quality_baseline.load_baseline(ws) or {}).get("nota")
        try:
            return float(anterior), True  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, True
    try:
        return float(bruto), False
    except ValueError:
        args.parser.error(f"--min-nota: esperado número ou 'baseline', veio {bruto!r}")
        raise AssertionError from None  # pragma: no cover - error() já saiu


def _pct(values: list[float], q: int) -> float:
    """Percentil por rank mais próximo — sem dependência, honesto para n pequeno."""
    ordered = sorted(values)
    idx = max(0, math.ceil(q / 100 * len(ordered)) - 1)
    return ordered[idx]


def cmd_bench(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    secs: list[float] = []
    for _ in range(args.n):
        t0 = time.monotonic()
        ws = provision(repo, f"bench-{uuid.uuid4().hex[:8]}")
        secs.append(time.monotonic() - t0)
        dispose(ws, keep=False)
    print(f"provision n={len(secs)} p50={_pct(secs, 50):.3f}s p95={_pct(secs, 95):.3f}s")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Tarefa em português vira unit autorada (em quarentena, nunca selada)."""
    from harness.add import ADD_MAX_USD, ADD_MODEL, AddError, add

    try:
        add(
            args.task,
            args.project,
            model=args.model or ADD_MODEL,
            max_usd=ADD_MAX_USD if args.max_usd is None else args.max_usd,
            dry=args.dry,
            ui=args.ui,
            projects_file=Path(args.projects) if args.projects else None,
            out_dir=Path(args.out_dir) if args.out_dir else None,
        )
    except AddError as exc:
        print(f"add falhou: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_decompose(args: argparse.Namespace) -> int:
    """Task grande vira fila de sub-units atômicas (a quebra que era humana)."""
    from harness.add import AddError
    from harness.improve.decompose import (
        DECOMPOSE_MAX_USD,
        DecomposeError,
        apply_decompose,
        planner_backend,
        propose_decompose,
    )

    try:
        backend, adapter = planner_backend(args.planner)
        proposal = propose_decompose(
            args.task,
            args.project,
            n_max=args.n_max,
            backend=backend,
            model=args.model,
            max_usd=DECOMPOSE_MAX_USD if args.max_usd is None else args.max_usd,
            projects_file=Path(args.projects) if args.projects else None,
            queue_dir=Path(args.queue_dir) if args.queue_dir else None,
            adapter=adapter,
        )
        if proposal is None:
            print(
                "decompose: plano inválido, reprovado no gate ou com menos de 2 passos "
                "— nada gravado",
                file=sys.stderr,
            )
            return 1
        apply_decompose(proposal, dry=args.dry)
    except (DecomposeError, AddError) as exc:
        print(f"decompose falhou: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_replan(args: argparse.Namespace) -> int:
    """Unidade travada: triagem por blocker + nota, repique só quando cabe."""
    from harness.add import AddError
    from harness.improve.decompose import DecomposeError
    from harness.improve.replan import replan

    try:
        return replan(
            args.project,
            args.unit,
            queue_dir=Path(args.queue_dir) if args.queue_dir else None,
            projects_file=Path(args.projects) if args.projects else None,
            n_max=args.n_max,
        )
    except (DecomposeError, AddError, FileNotFoundError, ValueError) as exc:
        print(f"replan falhou: {exc}", file=sys.stderr)
        return 1


def cmd_seal(args: argparse.Namespace) -> int:
    """Promove um exame da quarentena para sealed. Selar é ato humano."""
    from harness.improve import synthesize

    src = synthesize.QUARANTINE_DIR / args.name
    if not src.is_dir():
        print(
            f"seal: '{args.name}' não existe em {synthesize.QUARANTINE_DIR}",
            file=sys.stderr,
        )
        return 1
    if not args.yes:
        print(
            "seal: recusado — selar é ato humano e exige --yes. "
            "Revise o unit.toml antes de confirmar; nada foi movido.",
            file=sys.stderr,
        )
        return 1
    dst = synthesize.SEALED_DIR / args.name
    if dst.exists():
        print(f"seal: '{args.name}' já existe em {synthesize.SEALED_DIR}", file=sys.stderr)
        return 1
    # Currículo na fronteira: exame que a versão atual já passa não ensina nada.
    if not args.force:
        from harness.improve import coevolve
        from harness.improve.exam import frontier_backend

        # Backend explícito na mensagem: "fora da fronteira" só vale tanto quanto
        # o executor que screenou, e quem lê precisa saber qual foi.
        backend, model = frontier_backend()
        passed = coevolve.screen_benchmark(src, backend, model)
        if passed is True:
            print(
                f"seal: '{args.name}' fora da fronteira — o harness atual já passa "
                f"nesse exame sob backend={backend} model={model or '-'}, selar não "
                "ensina nada. Use --force para selar mesmo assim; nada foi movido.",
                file=sys.stderr,
            )
            return 1
        if passed is None:
            print(
                f"seal: screening de '{args.name}' inconclusivo (erro ao rodar) — "
                "seguindo com o selo por decisão humana (--yes).",
                file=sys.stderr,
            )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    print(f"selado: {dst}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Exame congelado de um artefato: congela, confere, relata, minera e sela.

    `verify` é o gate — violação imprime cada linha e sai != 0, porque medir
    contra um exame adulterado é pior que não medir. `seal-case` é o oposto do
    resto: o único subcomando que ESCREVE no bundle, e por isso o único que
    exige `--yes` (mesmo guard do `harness seal`).
    """
    from harness.evals import bundle_dir, freeze, verify_frozen
    from harness.evals.mining import mine, seal_case, write_pending
    from harness.evals.report import write_evaluation_md

    try:
        if args.action == "mine":
            propostas = mine(args.artifact, limit=args.limit, kind=args.kind or None)
            for p in propostas:
                print(f"{p.case_id}\t{p.source}\t{p.rationale}")
            print(write_pending(args.artifact, propostas))
            return 0
        if args.action == "seal-case":
            if not args.extra:
                print("eval seal-case: falta o <case-id>", file=sys.stderr)
                return 2
            if not args.yes:
                print(
                    "eval seal-case: recusado — selar é ato humano: use --yes. "
                    "Revise a proposta antes de confirmar; nada foi movido.",
                    file=sys.stderr,
                )
                return 2
            m = seal_case(args.artifact, args.extra)
            print(f"sealed {args.extra} -> manifest v{m.version}")
            return 0
        if args.action == "freeze":
            m = freeze(args.artifact, note=args.note)
            print(f"congelado: {bundle_dir(args.artifact)} v{m.version} {m.bundle_sha256[:12]}")
            return 0
        if args.action == "verify":
            violations = verify_frozen(args.artifact)
            for v in violations:
                print(v, file=sys.stderr)
            if violations:
                return 1
            print(f"ok: {bundle_dir(args.artifact)}")
            return 0
        print(write_evaluation_md(args.artifact))
        return 0
    except (OSError, ValueError) as exc:
        print(f"eval {args.action} falhou: {exc}", file=sys.stderr)
        return 1


def cmd_tune(args: argparse.Namespace) -> int:
    """Roda o tune loop offline contra o eval congelado e grava a evidência.

    Não aplica a versão vencedora no artefato — aplicação é ato do ciclo
    improve, com gate; aqui é bancada de medição.
    """
    from harness.improve.tune import TuneAborted, TuneError, run_tune

    try:
        outcome = run_tune(args.artifact, rounds=args.rounds, model=args.model)
    except TuneAborted as exc:
        print(f"tune abortado: {exc}", file=sys.stderr)
        return 1
    except TuneError as exc:
        print(f"tune falhou: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"tune falhou: {exc}", file=sys.stderr)
        return 1
    for v in outcome.chain:
        print(f"v{v.version} overall={v.agg.overall:.3f} {v.reason}")
    print(f"vencedor: v{outcome.winner}")
    return 0


def cmd_frontier(args: argparse.Namespace) -> int:
    """Lista a fronteira: os candidatos da quarentena em que o harness atual
    falha. Fronteira vazia é resposta, não erro — sai 0 sempre.

    `--backend`/`--model` vazios (default) leem `[frontier]`/`[exam]` do
    `config/ruler.toml`: o screening acompanha o executor da prova sem flag.
    """
    from harness.improve import coevolve
    from harness.improve.exam import MOCK_BACKEND, frontier_backend

    backend, model = frontier_backend()
    backend = args.backend or backend
    model = args.model or model
    names = coevolve.screen_quarantine(
        backend=backend,
        model=model,
        max_units=args.max_units,
        deadline_s=args.deadline,
    )
    for name in names:
        print(name)
    real = backend != MOCK_BACKEND
    print(f"frontier={len(names)} real={'true' if real else 'false'}")
    if not real:
        print(
            "frontier: screening mock não discrimina dificuldade — o mock falha em "
            "quase tudo, então esta fronteira satura. Aponte [frontier] em "
            "config/ruler.toml (ou --backend) para o executor da prova.",
            file=sys.stderr,
        )
    return 0


def cmd_evolve(args: argparse.Namespace) -> int:
    """`--steps` gerações de PBT com fitness real: cada indivíduo é um braço de
    execução, a nota é o Wilson lower bound do gate e o melhor de cada nicho vai
    para o archive (MAP-Elites).

    Sai 0 com qualquer nota, inclusive 0/n: "essa config não passa" é resposta
    da evolução, não erro da CLI — mesma regra do `harness ab`.

    Default `--backend mock`: a evolução é o laço que roda mais vezes
    (steps x pop x n runs), e ligá-la por engano num backend pago é o jeito mais
    rápido de queimar dinheiro sem ninguém olhando.
    """
    from harness.evolve.archive import Archive
    from harness.evolve.fitness import Fitness, evolve

    try:
        units = [load_unit(p) for p in _improve_units(args)]
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    fitness = Fitness(
        units=units,
        n=args.n,
        data_dir=store.data_dir(),
        project=args.project,
    )
    archive = Archive(Path(args.archive) if args.archive else None)
    try:
        report = evolve(
            fitness,
            {"backend": args.backend, "model": args.model, "max_turns": args.max_turns},
            archive,
            steps=args.steps,
            pop_size=args.pop,
            seed=args.seed,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        archive.close()

    best = report.best
    for ind in report.population:
        print(
            f"ind max_turns={ind.config.get('max_turns')} "
            f"{ind.successes}/{ind.trials} wilson_low={ind.wilson_low:.2f}"
        )
    print(
        # `genomas` e não "avaliações": genoma repetido na população compartilha
        # a chave do stats, e chamar isso de avaliação inflaria a conta.
        f"evolve steps={report.steps} pop={args.pop} genomas={len(fitness.stats)} "
        f"best={best.successes}/{best.trials} wilson_low={best.wilson_low:.2f} "
        f"elites={len(report.elites)} archive={archive.path}"
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """`harness init <repo> --name <nome>` registra um repo real como projeto."""
    from harness.projects import init_project

    try:
        proj = init_project(
            args.repo,
            args.name,
            build_cmd=args.build,
            verify_default=args.verify_default,
            queue_dir=args.queue_dir,
            setup_cmd=args.setup_cmd,
            setup_timeout=args.setup_timeout,
            env_file=args.env_file,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"projeto {proj.name}: repo={proj.repo} queue={proj.queue_dir}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Uma linha por projeto: fila/done/stuck + gasto total do ledger. Projeto
    com `MILESTONES.toml` ganha uma linha indentada por marco — dado opcional,
    quem não declara marco vê a saída de antes."""
    from harness.projects import load_projects, milestone_progress, queue_counts

    projs = load_projects()
    if not projs:
        print("nenhum projeto registrado (harness init <repo> --name <nome>)")
        return 0

    for name in sorted(projs):
        fila, done, stuck = queue_counts(projs[name])
        rows = store.history(project=name, limit=100_000) if store.db_path().is_file() else []
        usd = sum(r.cost_usd or 0.0 for r in rows)
        print(f"{name}: fila={fila} done={done} stuck={stuck} runs={len(rows)} usd={usd:.2f}")
        for marco, feitas, total in milestone_progress(projs[name]):
            print(f"  {'✔' if feitas == total else '○'} {marco} ({feitas}/{total})")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """`harness queue --project <nome>` consome a fila do projeto pelo grafo."""
    from harness import queue as queue_mod

    try:
        return queue_mod.run_queue(
            args.project,
            backend=args.backend,
            model=args.model,
            deadline_s=args.deadline_s,
            attempts=args.attempts,
            move=args.move,
            integrate_accepted=args.integrate,
            check_regression=args.regression,
            use_zpd=args.zpd,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1


def cmd_webhook(args: argparse.Namespace) -> int:
    """`harness webhook --port N`: a porta HTTP que deposita eventos no inbox.

    Bind sempre em 127.0.0.1 (quem decide é o `serve_webhook`): expor para a
    rede é trabalho de proxy reverso, não de flag nossa. Sem token o servidor
    SOBE recusando tudo com 403 e o `serve_webhook` imprime o `NO_TOKEN_HELP` —
    serviço que morre calado esconde a config faltando. Ctrl-C sai 0: parar um
    vigia não é erro.
    """
    from harness.triggers.webhook import serve_webhook

    inbox = store.data_dir() / "inbox"
    try:
        serve_webhook(
            args.port,
            inbox,
            on_bind=lambda p: print(f"[webhook] 127.0.0.1:{p} -> {inbox}"),
        )
    except KeyboardInterrupt:
        print("[webhook] parado", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Auto-relatório do loop em markdown. Fail-open: sempre exit 0."""
    from harness import report as report_mod

    text = report_mod.build_report(since_hours=args.since, db_path=args.db, lineage_file=args.file)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"report {out}")
    else:
        print(text, end="")
    return 0


# Duas prateleiras, e não uma lista alfabética de 29 subcomandos: quem chega
# hoje não tem como saber que `do` é o começo e `evolve` é o fim. A separação é
# só de apresentação — nenhum comando muda de comportamento por estar aqui.
EPILOG = """\
BÁSICO      quickstart · do · status · report
AVANÇADO    run · queue · add · decompose · replan · ab · improve · evolve ·
            frontier · seal · eval · tune · replay · whatif · lineage · skills ·
            actions · procs · cache-gc · webhook · bench · ui-verify ·
            vision-judge · init · export · import · doctor · backends

Detalhe de qualquer um: harness <comando> --help
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness",
        description="agent harness provider-agnostic",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    from harness.add import KINDS  # vocabulário de kind, num lugar só

    quickstart = sub.add_parser(
        "quickstart", help="[BÁSICO] o que instalar, o que rodar, e o que está faltando"
    )
    quickstart.set_defaults(func=cmd_quickstart, parser=quickstart)

    d = sub.add_parser("do", help="[BÁSICO] faz o pedido na pasta atual (o default decide tudo)")
    d.add_argument("task", help='o pedido em português, ex.: "conserta o bug em target.py"')
    # Nada de SUPPRESS: flag escondida é flag que ninguém descobre, e a promessa
    # do `do` é que o default resolve — não que não haja volante.
    adv = d.add_argument_group("avançado (opcional — o default decide sozinho)")
    adv.add_argument(
        "--verify-cmd",
        default=None,
        dest="verify_cmd",
        metavar="CMD",
        help="a régua que prova que ficou pronto; sem ela o harness detecta "
        "pela cara do repo (pytest, npm test, make test, cargo, go)",
    )
    adv.add_argument(
        "--kind",
        default=None,
        choices=list(KINDS),
        help="que tipo de trabalho é; sem ela o router classifica pelo pedido",
    )
    adv.add_argument(
        "--route",
        default=None,
        choices=list(ROUTE_MODES),
        help="auto (default): o router escolhe executor e modelo; manual: você escolhe",
    )
    adv.add_argument("--backend", default=None, help="força o executor (implica --route manual)")
    adv.add_argument("--model", default=None, help="força o modelo (implica --route manual)")
    adv.add_argument("--max-turns", type=int, default=None, dest="max_turns", metavar="N")
    adv.add_argument(
        "--no-apply",
        action="store_true",
        dest="no_apply",
        help="não faz o merge no fim; o resultado fica na branch de entrega",
    )
    adv.add_argument(
        "--keep-unit",
        action="store_true",
        dest="keep_unit",
        help="mantém .harness/units/<id> depois do run (para inspeção)",
    )
    adv.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="mostra régua, rota e a unidade que seria rodada, e para aí",
    )
    d.set_defaults(func=cmd_do, parser=d, project=None, repo=None)

    run = sub.add_parser("run", help="[AVANÇADO] executa uma unidade com um backend")
    run.add_argument("--unit", required=True, help="diretório (ou arquivo) com unit.toml")
    run.add_argument("--backend", default=None, help="obrigatório sem --route auto")
    run.add_argument("--model", default=None)
    run.add_argument(
        "--route",
        choices=list(ROUTE_MODES),
        default=ROUTE_MANUAL,
        help="auto: o router escolhe tier/backend/model pelo kind da "
        "unidade e pelo histórico do ledger (exclui --backend/--model)",
    )
    run.add_argument("--project", default=None)
    run.add_argument(
        "--repo",
        default=None,
        help="repo-alvo (git, limpo): vira o workspace e é onde os KPIs "
        "são medidos; sem ele o run roda num tmpdir vazio",
    )
    # default None (e não DEFAULT_MAX_TURNS): é assim que `--route auto` sabe
    # distinguir "o usuário não pediu turnos" de "o usuário pediu 8".
    run.add_argument("--max-turns", type=int, default=None, dest="max_turns")
    run.set_defaults(func=cmd_run, parser=run)

    ab = sub.add_parser("ab", help="veredito de Wilson entre dois braços")
    ab.add_argument(
        "--a", type=_arm, metavar="SUCC/N", help="braço A (baseline) já contado, ex.: 5/6"
    )
    ab.add_argument(
        "--b", type=_arm, metavar="SUCC/N", help="braço B (candidata) já contado, ex.: 6/6"
    )
    ab.add_argument(
        "--min-n",
        type=int,
        default=MIN_N,
        dest="min_n",
        help=f"tentativas por braço para a régua opinar (default {MIN_N})",
    )
    # Modo de execução: o harness roda o experimento em vez de só contar.
    ab.add_argument(
        "--dim",
        choices=["backend"],
        default=None,
        help="dimensão testada; roda a mesma unidade nos dois braços",
    )
    ab.add_argument("--unit", default=None, help="diretório (ou arquivo) com unit.toml")
    ab.add_argument("--a-backend", default=None, dest="a_backend")
    ab.add_argument("--b-backend", default=None, dest="b_backend")
    ab.add_argument("--a-model", default=None, dest="a_model")
    ab.add_argument("--b-model", default=None, dest="b_model")
    ab.add_argument("--n", type=int, default=MIN_N, help=f"tentativas por braço (default {MIN_N})")
    ab.add_argument("--project", default=None)
    # `parser` no namespace: erro de combinação de flag sai como erro de
    # argparse (uso + exit 2), não como exceção no meio do experimento.
    ab.set_defaults(func=cmd_ab, parser=ab)

    init = sub.add_parser(
        "init", help="[AVANÇADO] registra um repo git real como projeto (config/projects.toml)"
    )
    init.add_argument("repo", help="path do repositório git do projeto")
    init.add_argument("--name", required=True)
    init.add_argument(
        "--build",
        default=None,
        help="comando de build do projeto; roda antes do verify_cmd da unidade, no worktree",
    )
    init.add_argument(
        "--verify-default",
        default=None,
        dest="verify_default",
        help="verify_cmd default para unidade do projeto que não declara um",
    )
    init.add_argument(
        "--queue-dir",
        default=None,
        dest="queue_dir",
        help="fila do projeto (default projects/<nome>/queue)",
    )
    init.add_argument(
        "--setup-cmd",
        default=None,
        dest="setup_cmd",
        help="comando de setup (instalar dependência) do workspace; "
        "roda antes do executor, cacheado por lockfile. Sem ele, "
        "detecção automática (npm ci / uv sync)",
    )
    init.add_argument(
        "--setup-timeout",
        type=int,
        default=SETUP_TIMEOUT,
        dest="setup_timeout",
        help=f"teto em segundos do setup (default {SETUP_TIMEOUT})",
    )
    init.add_argument(
        "--env-file",
        default=None,
        dest="env_file",
        help="env do projeto (ex.: .env), path RELATIVO ao repo; entra "
        "no env dos subprocessos do run. Valor de segredo é "
        "redigido dos logs",
    )
    init.set_defaults(func=cmd_init, parser=init)

    status = sub.add_parser("status", help="por projeto: fila/done/stuck + gasto total do ledger")
    status.set_defaults(func=cmd_status, parser=status)

    from harness import queue as queue_mod  # defaults do driver, num só lugar

    queue = sub.add_parser(
        "queue", help="consome a fila do projeto pelo grafo, uma unidade por vez"
    )
    queue.add_argument(
        "--project",
        default=queue_mod.DEFAULT_PROJECT,
        help="nome do projeto registrado (harness init)",
    )
    queue.add_argument("--backend", default=queue_mod.DEFAULT_BACKEND)
    queue.add_argument(
        "--model", default=queue_mod.DEFAULT_MODEL, help="vazio ('') usa o default do backend"
    )
    queue.add_argument(
        "--deadline-s",
        type=float,
        dest="deadline_s",
        default=queue_mod.DEFAULT_DEADLINE_S,
        help="teto de tempo do loop inteiro",
    )
    queue.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="tentativas por unidade (default: teto de config/graph.toml)",
    )
    queue.add_argument(
        "--move",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="--no-move é ensaio: roda e não mexe na fila",
    )
    queue.add_argument(
        "--integrate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="merge da entrega aceita no branch default (default: "
        "ligado). --no-integrate quebra a fila progressiva: a "
        "unidade seguinte sai do HEAD e não vê a anterior",
    )
    queue.add_argument(
        "--regression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="re-roda o verify das unidades de done/ no repo depois "
        "de cada integração (default: ligado). --no-regression "
        "deixa conflito semântico passar silencioso",
    )
    queue.add_argument(
        "--zpd",
        action="store_true",
        help="começa pela unidade com nota histórica na zona de "
        "desenvolvimento proximal (0.4-0.8) em vez da ordem de "
        "nome (default: desligado). Em fila de PROJETO a ordem "
        "de nome é a dependência e reordenar quebra a fila; use "
        "só em fila de prática/benchmark, onde as unidades são "
        "independentes",
    )
    queue.set_defaults(func=cmd_queue, parser=queue)

    backends = sub.add_parser("backends", help="lista backends registrados + preflight")
    backends.set_defaults(func=cmd_backends)

    improve = sub.add_parser("improve", help="ciclo de auto-melhoria: muta config e testa em A/B")
    improve.add_argument("--cycles", type=int, default=1)
    improve.add_argument(
        "--deadline-s",
        type=float,
        default=None,
        dest="deadline_s",
        help="teto de tempo do loop; estourou, escala pro humano",
    )
    improve.add_argument(
        "--unit",
        action="append",
        default=[],
        help="unidade de avaliação (repetível); default: benchmarks/held_in/*",
    )
    improve.add_argument(
        "--backend",
        default=None,
        help="fixa o executor dos DOIS braços; default: quem escolhe é o "
        "router, lendo o config que a mutação acabou de mexer",
    )
    improve.add_argument("--model", default=None)
    improve.add_argument(
        "--n",
        type=int,
        default=None,
        help="tentativas por braço (default: [improve].n_per_arm do catalog)",
    )
    improve.add_argument(
        "--resume",
        default=None,
        metavar="THREAD_ID",
        help="responde a escalação pendente da thread (o id sai no "
        "`escalate ... thread=` do stderr)",
    )
    improve.add_argument(
        "--answer",
        default=None,
        metavar="JSON",
        help=f"resposta do humano ao interrupt, só com --resume (default: {IMPROVE_ANSWER})",
    )
    improve.set_defaults(func=cmd_improve, parser=improve)

    replay = sub.add_parser("replay", help="atribui delta do histórico a uma mutação")
    replay.add_argument(
        "--mutation", default=None, metavar="ID", help="id da mutação (os ids saem no --list)"
    )
    replay.add_argument(
        "--list", action="store_true", help="lista as mutações do ledger com veredito"
    )
    replay.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"teto de linhas lidas do ledger, nos DOIS modos (default {DEFAULT_LIMIT})",
    )
    replay.set_defaults(func=cmd_replay, parser=replay)

    whatif = sub.add_parser("whatif", help="re-roda os fracassos do ledger com a config de hoje")
    whatif.add_argument(
        "--kind", default=None, metavar="K", help="só fracassos deste kind (default: todos)"
    )
    whatif.add_argument(
        "--limit",
        type=int,
        default=WHATIF_LIMIT,
        metavar="N",
        help=f"quantas unidades re-rodar (default {WHATIF_LIMIT})",
    )
    whatif.add_argument(
        "--backend",
        default=MOCK_BACKEND,
        metavar="B",
        help=f"executor do replay (default {MOCK_BACKEND})",
    )
    whatif.add_argument("--model", default=None, metavar="M", help="modelo do backend")
    whatif.set_defaults(func=cmd_whatif)

    lineage = sub.add_parser("lineage", help="árvore genealógica das mutações de código")
    lineage.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="jsonl de linhagem (default data/lineage.jsonl)",
    )
    lineage.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="banco do ledger p/ veredito (default data/runs.sqlite)",
    )
    lineage.add_argument(
        "--limit", type=int, default=None, metavar="N", help="mostra só as N últimas raízes"
    )
    lineage.set_defaults(func=cmd_lineage)

    report = sub.add_parser(
        "report", help="auto-relatório do loop (runs, mutações, skills, linhagem)"
    )
    report.add_argument(
        "--since",
        type=float,
        default=REPORT_SINCE,
        metavar="HORAS",
        help=f"janela de tempo (default {REPORT_SINCE:g}h)",
    )
    report.add_argument(
        "--out", default=None, metavar="PATH", help="grava o markdown no arquivo em vez do stdout"
    )
    report.add_argument(
        "--file",
        default=None,
        metavar="PATH",
        help="jsonl de linhagem (default data/lineage.jsonl)",
    )
    report.add_argument(
        "--db", default=None, metavar="PATH", help="banco do ledger (default data/runs.sqlite)"
    )
    report.set_defaults(func=cmd_report)

    ui = sub.add_parser(
        "ui-verify",
        help="verify de UI: serve o dist, confere os assets e olha o screenshot",
    )
    ui.add_argument("dist", help="diretório buildado a servir (ex.: dist)")
    ui.add_argument(
        "--url-path",
        default="/",
        dest="url_path",
        help="página a abrir dentro do dist (default: /)",
    )
    ui.add_argument(
        "--min-kb",
        type=float,
        default=DEFAULT_MIN_KB,
        dest="min_kb",
        help=f"tamanho mínimo do PNG em kb (default {DEFAULT_MIN_KB:.0f}; "
        "tela em branco mede ~11kb, página com conteúdo ~28kb)",
    )
    ui.add_argument(
        "--expect-asset",
        action="append",
        choices=list(ASSET_KINDS),
        dest="expect_asset",
        metavar="KIND",
        help=f"exige ≥1 asset do tipo CARREGÁVEL (repetível): {'|'.join(ASSET_KINDS)}",
    )
    ui.add_argument(
        "--strict-links",
        action="store_true",
        dest="strict_links",
        help="<a href> local morto REPROVA em vez de avisar (gate de "
        "completude: use na unidade que fecha o site)",
    )
    ui.add_argument(
        "--shot-out",
        default=None,
        dest="shot_out",
        metavar="PATH",
        help=f"onde gravar o screenshot (default: ./{SHOT_NAME}, que "
        "sobrevive com --keep-ws para review humano)",
    )
    ui.add_argument(
        "--ask",
        default=None,
        metavar="PERGUNTA",
        help="opt-in que GASTA (~$0.01): manda o screenshot para o "
        'claude CLI em haiku e exige JSON {"ok","motivo"}',
    )
    ui.set_defaults(func=cmd_ui_verify, parser=ui)

    vj = sub.add_parser(
        "vision-judge",
        help="subcheck de UI: um VLM local olha o screenshot e dá nota (fail-open)",
    )
    alvo = vj.add_mutually_exclusive_group(required=True)
    alvo.add_argument(
        "--port",
        type=int,
        default=None,
        help="porta de um servidor registrado nesta run (start_server)",
    )
    alvo.add_argument(
        "--dist",
        default=None,
        metavar="PATH",
        help="diretório buildado a servir em loopback (ex.: dist)",
    )
    vj.add_argument(
        "--question",
        default=None,
        metavar="PERGUNTA",
        help="foca o olhar do juiz (ex.: 'o menu está alinhado?')",
    )
    vj.add_argument(
        "--min-nota",
        default="6.0",
        dest="min_nota",
        metavar="N|baseline",
        help="piso da nota 0-10 (default 6.0). `baseline` usa a nota "
        "aceita da última vez — régua relativa, anti-platô",
    )
    vj.add_argument(
        "--ref",
        default=None,
        metavar="PNG",
        help="compara PAREADO com esta referência em vez de dar nota "
        "absoluta (mais confiável em VLM pequeno): passa se a tela "
        "nova ganhar",
    )
    vj.add_argument(
        "--ws",
        default=None,
        metavar="PATH",
        help="workspace (default: diretório atual) — onde ficam procs.json, os shots e o baseline",
    )
    vj.set_defaults(func=cmd_vision_judge, parser=vj)

    export = sub.add_parser(
        "export", help="empacota skills + prior de roteamento em um bundle .tgz"
    )
    export.add_argument("--out", required=True, metavar="PATH", help="destino do bundle (tar.gz)")
    export.set_defaults(func=cmd_export)

    # `import` é keyword: o parser aceita o nome, o dest do handler não pode ser.
    import_bundle = sub.add_parser(
        "import", help="traz skills + prior de um bundle de outro projeto"
    )
    import_bundle.add_argument("bundle", help="caminho do bundle .tgz")
    import_bundle.set_defaults(func=cmd_import_bundle)

    doctor = sub.add_parser(
        "doctor", help="diagnóstico local: backends, genoma, config, dados, tracing"
    )
    doctor.set_defaults(func=cmd_doctor, parser=doctor)

    skills = sub.add_parser("skills", help="lista as skills carregadas (nome, kinds, descrição)")
    skills.add_argument(
        "--lift",
        action="store_true",
        help="anexa o lift por skill (atribuição do ledger; sem amostra num braço = traço)",
    )
    skills.set_defaults(func=cmd_skills)

    actions = sub.add_parser("actions", help="lista as ações do registry + KEEP/DISCARD do ledger")
    actions.set_defaults(func=cmd_actions)

    procs_cmd = sub.add_parser("procs", help="lista servidores registrados nos workspaces dos runs")
    procs_cmd.add_argument(
        "--reap",
        action="store_true",
        help="mata os processos cujo run já morreu (órfãos); run vivo nunca é tocado",
    )
    procs_cmd.set_defaults(func=cmd_procs)

    cache = sub.add_parser(
        "cache-gc", help="poda o cache de dependência (uv/npm) até caber no teto"
    )
    cache.add_argument(
        "--max-gb",
        type=float,
        default=cache_gc.DEFAULT_MAX_GB,
        dest="max_gb",
        help=f"teto em GB (default {cache_gc.DEFAULT_MAX_GB:g})",
    )
    cache.add_argument(
        "--dry-run", action="store_true", help="só reporta o uso atual, não remove nada"
    )
    cache.set_defaults(func=cmd_cache_gc)

    add_cmd = sub.add_parser(
        "add", help="autora uma unit a partir de uma tarefa em linguagem natural"
    )
    add_cmd.add_argument("task", help="a tarefa, escrita em português")
    add_cmd.add_argument(
        "--project", required=True, help="projeto registrado em config/projects.toml"
    )
    add_cmd.add_argument(
        "--dry", action="store_true", help="mostra a unit autorada sem gravar nada"
    )
    add_cmd.add_argument(
        "--ui",
        action="store_true",
        help="tarefa de frontend: gruda `harness ui-verify dist "
        "--expect-asset css` no verify_cmd autorado",
    )
    add_cmd.add_argument("--model", default=None, help="modelo da autoria (default: haiku)")
    add_cmd.add_argument(
        "--max-usd",
        type=float,
        default=None,
        dest="max_usd",
        help="teto de custo da chamada de autoria (default: 0.25)",
    )
    add_cmd.add_argument("--projects", default=None, help="registro de projetos alternativo")
    add_cmd.add_argument(
        "--out-dir",
        default=None,
        dest="out_dir",
        help="destino da unit (default: benchmarks/quarantine)",
    )
    add_cmd.set_defaults(func=cmd_add)

    # Import tardio como no cmd_decompose: só o teto do plano é preciso aqui, e
    # o módulo puxa backends/registry — quem nunca decompõe não paga isso.
    from harness.improve.decompose import DEFAULT_N_MAX, PLANNERS

    dec = sub.add_parser(
        "decompose",
        help="quebra uma tarefa grande numa fila ordenada de sub-units atômicas",
    )
    dec.add_argument("task", help="a tarefa grande, escrita em português")
    dec.add_argument("--project", required=True, help="projeto registrado em config/projects.toml")
    dec.add_argument(
        "--n-max",
        type=int,
        default=DEFAULT_N_MAX,
        dest="n_max",
        help=f"teto de passos do plano (default {DEFAULT_N_MAX})",
    )
    dec.add_argument("--dry", action="store_true", help="mostra a fila planejada sem gravar nada")
    dec.add_argument(
        "--planner",
        choices=PLANNERS,
        default=PLANNERS[0],
        help="quem planeja: remote (default) ou local (frota local + adapter de raciocínio)",
    )
    dec.add_argument("--model", default=None, help="modelo do planejamento (default: haiku)")
    dec.add_argument(
        "--max-usd",
        type=float,
        default=None,
        dest="max_usd",
        help="teto de custo da chamada de planejamento",
    )
    dec.add_argument("--projects", default=None, help="registro de projetos alternativo")
    dec.add_argument(
        "--queue-dir",
        default=None,
        dest="queue_dir",
        help="fila destino (default: a do projeto no registro)",
    )
    dec.set_defaults(func=cmd_decompose)

    from harness.improve.replan import REPLAN_N_MAX

    rep = sub.add_parser(
        "replan",
        help="unidade travada: roteia por blocker/nota e repica só ela quando cabe",
    )
    rep.add_argument("--project", required=True, help="projeto registrado em config/projects.toml")
    rep.add_argument("--unit", required=True, help="id da unidade (o nome do dir na fila)")
    rep.add_argument(
        "--n-max",
        type=int,
        default=REPLAN_N_MAX,
        dest="n_max",
        help=f"teto de sub-passos do repique (default {REPLAN_N_MAX})",
    )
    rep.add_argument("--projects", default=None, help="registro de projetos alternativo")
    rep.add_argument(
        "--queue-dir",
        default=None,
        dest="queue_dir",
        help="fila do projeto (default: a do registro)",
    )
    rep.set_defaults(func=cmd_replan)

    seal = sub.add_parser("seal", help="promove um exame da quarentena para benchmarks/sealed")
    seal.add_argument("name", help="nome do dir em benchmarks/quarantine")
    seal.add_argument(
        "--force",
        action="store_true",
        help="sela mesmo que o harness atual já passe no exame (fora da fronteira de dificuldade)",
    )
    seal.add_argument(
        "--yes", action="store_true", help="confirmação humana; sem isto o comando recusa"
    )
    seal.set_defaults(func=cmd_seal)

    ev = sub.add_parser("eval", help="exame congelado de um artefato: freeze, verify, report, mine")
    ev.add_argument(
        "action",
        choices=("freeze", "verify", "report", "mine", "seal-case"),
        help=(
            "freeze congela o bundle, verify prova que ele não mudou, report escreve "
            "EVALUATION.md, mine propõe casos do ledger, seal-case sela um caso "
            "proposto (--yes)"
        ),
    )
    ev.add_argument("artifact", help="o artefato avaliado, ex.: skills/python-fixes.md")
    # Positional extra em vez de um subparser por ação: `freeze/verify/report`
    # já são posicionais de uma palavra, e o `seal-case` é o único que precisa de
    # um segundo argumento. `nargs="?"` deixa os três antigos intactos.
    ev.add_argument("extra", nargs="?", default=None, help="<case-id>, só no seal-case")
    ev.add_argument("--note", default="", help="nota da versão congelada (só no freeze)")
    ev.add_argument("--limit", type=int, default=20, help="teto de propostas do mine")
    ev.add_argument("--kind", default="", help="filtra a mineração por kind")
    ev.add_argument(
        "--yes", action="store_true", help="confirmação humana do seal-case; sem isto ele recusa"
    )
    ev.set_defaults(func=cmd_eval)

    tune = sub.add_parser("tune", help="tune loop offline: score → rewrite → gate monotônico")
    tune.add_argument("artifact", help="artefato tunável, ex.: skills/python-fixes.md")
    tune.add_argument("--rounds", type=int, default=2, help="rodadas de rewrite (default 2)")
    tune.add_argument("--model", default="openai:qwen/qwen3.5-9b", help="modelo do rewriter/runner")
    tune.set_defaults(func=cmd_tune)

    frontier = sub.add_parser(
        "frontier", help="lista os exames da quarentena em que o harness atual falha"
    )
    frontier.add_argument(
        "--backend",
        default="",
        help="backend do screening (default: [frontier]/[exam] de config/ruler.toml)",
    )
    frontier.add_argument("--model", default="", help="modelo do backend (default: o do config)")
    frontier.add_argument(
        "--max-units",
        type=int,
        default=0,
        dest="max_units",
        help="teto de candidatos screenados (0 = sem teto)",
    )
    frontier.add_argument(
        "--deadline", type=float, default=0.0, help="teto de segundos do screening (0 = sem teto)"
    )
    frontier.set_defaults(func=cmd_frontier)

    evolve = sub.add_parser(
        "evolve", help="PBT de configs: mede cada indivíduo no gate e arquiva os elites"
    )
    evolve.add_argument("--steps", type=int, default=1, help="gerações (default 1)")
    evolve.add_argument("--pop", type=int, default=4, help="tamanho da população (default 4)")
    evolve.add_argument(
        "--n", type=int, default=1, help="runs por indivíduo POR unidade (default 1)"
    )
    evolve.add_argument(
        "--unit",
        action="append",
        default=[],
        help="unidade de avaliação (repetível); default: benchmarks/held_in/*",
    )
    evolve.add_argument(
        "--backend",
        default="mock",
        help="executor de TODA a população; default mock ($0, "
        "determinístico) — a evolução é o laço que roda mais vezes",
    )
    evolve.add_argument("--model", default=None)
    evolve.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        dest="max_turns",
        help=f"knob mutável do genoma (default {DEFAULT_MAX_TURNS})",
    )
    evolve.add_argument(
        "--seed",
        type=int,
        default=0,
        help="semente do PBT: mesma semente, mesma população (default 0)",
    )
    evolve.add_argument(
        "--archive",
        default=None,
        metavar="PATH",
        help="sqlite do MAP-Elites (default data/archive.sqlite)",
    )
    evolve.add_argument("--project", default=None)
    evolve.set_defaults(func=cmd_evolve, parser=evolve)

    webhook = sub.add_parser(
        "webhook",
        help="sobe a porta HTTP (loopback) que deposita eventos no inbox",
    )
    webhook.add_argument(
        "--port",
        type=int,
        default=WEBHOOK_PORT,
        help=f"porta em 127.0.0.1 (default {WEBHOOK_PORT}; 0 = efêmera, o bind é impresso)",
    )
    webhook.set_defaults(func=cmd_webhook)

    bench = sub.add_parser("bench", help="mede o custo de uma operação do harness")
    bench.add_argument("what", choices=["provision"])
    bench.add_argument("--n", type=int, default=10)
    bench.add_argument("--repo", default=".", help="repositório git de origem")
    bench.set_defaults(func=cmd_bench)

    return p


def main(argv: list[str] | None = None) -> int:
    _bootstrap()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
