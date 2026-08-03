"""CLI do harness. `harness run` / `ab` / `improve` / `replay` / `lineage` /
`doctor` / `backends` / `seal` / `bench`."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from harness.ab import ArmSpec, run_ab
from harness.backends import registry
from harness.improve.replay import DEFAULT_LIMIT
from harness.ledger import store
from harness.routing import ROUTE_AUTO, ROUTE_MANUAL, ROUTE_MODES, router
from harness.ruler.gate import Decision, gate
from harness.ruler.kpi import collect, load_kpis
from harness.ruler.verify import run_verify
from harness.ruler.wilson import MIN_N, Arm, decide_ab, wilson_interval
from harness.types import ExecRequest, ExecResult, RunRow, Selection, UnitSpec
from harness.workspace.provision import dispose, provision

UNIT_FILE = "unit.toml"
SCRATCH_DIR = ".harness"   # log do verify; não conta como sujeira do repo-alvo
DEFAULT_MAX_TURNS = 8
HELD_IN = Path("benchmarks/held_in")   # unidades default do `harness improve`
# Resposta default do `--resume`: abortar. Retomar um loop sem dizer o que
# fazer não pode significar "continua sozinho" — quem foi chamado tem que
# escolher explicitamente continuar.
IMPROVE_ANSWER = '{"action":"abort"}'


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
    missing = [k for k in ("id", "prompt", "verify_cmd") if k not in data]
    if missing:
        raise ValueError(f"{unit_file}: campos faltando: {', '.join(missing)}")
    return UnitSpec(
        id=str(data["id"]),
        path=unit_file.parent,
        prompt=str(data["prompt"]),
        verify_cmd=str(data["verify_cmd"]),
        kind=data.get("kind"),
    )


def seed_workspace(unit: UnitSpec, ws: Path) -> list[str]:
    """Copia os arquivos da unidade pro workspace (menos o próprio `unit.toml`)."""
    copied: list[str] = []
    for src in sorted(unit.path.rglob("*")):
        rel = src.relative_to(unit.path)
        if rel.parts[0] == UNIT_FILE or "__pycache__" in rel.parts:
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
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _dirty(repo: Path) -> list[str]:
    """Linhas de `git status --porcelain`, menos o scratch da própria régua."""
    proc = _git(repo, "status", "--porcelain")
    if proc.returncode != 0:
        raise ValueError(f"{repo} não é um repo git: {proc.stderr.strip()}")
    prefix = SCRATCH_DIR + "/"
    return [
        ln for ln in proc.stdout.splitlines()
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
            print(f"revert: git {' '.join(cmd)} falhou — {proc.stderr.strip()}",
                  file=sys.stderr)


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
        return "done"               # mesmo que o executor tenha estourado turnos
    if decision.reason.startswith("backend_"):
        return result.exit_reason   # nem chegou a executar (blocked/error)
    if decision.action == "retry":
        return "verify_failed"      # única outra causa de retry no gate
    return decision.reason          # revert carrega kpi_regression:… / tamper:…


class PreflightError(RuntimeError):
    """Backend indisponível: nada executou, então não há linha honesta pro ledger."""


@dataclass(frozen=True)
class RunOutcome:
    """O que um run produziu. A `Decision` viaja junto porque `RunRow` só guarda
    o `exit_reason`, que colapsa a ação da régua (`revert` e `retry` viram uma
    causa só) — quem imprime precisa dos dois."""

    row: RunRow
    decision: Decision


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
        result = backend.execute(
            ExecRequest(
                prompt=unit.prompt,
                workspace=ws,
                model=model,
                max_turns=max_turns,
                trace_path=ws / "trace.jsonl",
                run_id=run_id,
            )
        )
        # A régua decide, não o executor: verify roda sempre que houve execução
        # (mesmo max_turns/timeout — pode ter consertado antes de estourar).
        # "blocked"/"error" nem chegaram a executar; aí não há o que verificar.
        ran = result.exit_reason in ("done", "max_turns", "timeout")
        if ran:
            verdict = run_verify(unit, ws)
            # `specs=` do ANTES: a mudança avaliada não pode redefinir a régua
            # reescrevendo o kpis.toml (buraco de Goodhart do review do PR-4).
            decision = gate(verdict, kpi_before, collect(ws, specs=specs), [], specs)
        else:
            decision = Decision("retry", f"backend_{result.exit_reason}")
        if decision.action == "revert" and repo is not None:
            _revert(ws)   # no tmpdir o revert é o próprio descarte

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
        intervention=False,
        created_at=store.now_iso(),
    )
    return RunOutcome(row=row, decision=decision)


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


def cmd_run(args: argparse.Namespace) -> int:
    unit = load_unit(Path(args.unit))
    sel = _resolve_route(args, unit)
    if args.route == ROUTE_AUTO:
        print(
            f"route auto {unit.id} kind={sel.kind} tier={sel.tier} "
            f"{sel.backend} {sel.model or '-'} [{' '.join(sel.reasons)}]"
        )
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
    print(
        f"{row.run_id} {unit.id} {row.backend} {decision.action} {decision.reason} "
        f"{row.sec_total:.2f}s ledger#{row_id}"
    )
    return 0 if row.ok else 1


def _arm(text: str) -> Arm:
    """`sucessos/tentativas` -> Arm. É o formato de `harness ab --a 5/6`."""
    succ_raw, sep, n_raw = text.partition("/")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"esperado sucessos/tentativas (ex.: 5/6), veio {text!r}"
        )
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
    if args.resume:
        raw = args.answer if args.answer is not None else IMPROVE_ANSWER
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError as exc:
            args.parser.error(f"--answer não é JSON: {exc}")
        if not isinstance(answer, dict):
            args.parser.error(f"--answer tem que ser um objeto JSON: {raw!r}")
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
        for name, value in zip(("kind", "tier", "backend"), att.key)
        if value
    )
    delta = "n/a" if att.delta is None else f"{att.delta:+.2f}"
    leitura = (
        "sem amostra" if att.delta is None
        else "separados" if att.separated
        else "sobrepostos"
    )
    nomes = " ".join(
        f"{c.mutation_id}:{c.rule_id}@{c.applied_at}" for c in att.confounders
    )
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
                f"{'revertida' if m.reverted else 'mantida'}"
                + (f" ({m.note})" if m.note else "")
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
        tree = tree[-args.limit:]
    print(lineage.render(tree))
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


def cmd_actions(args: argparse.Namespace) -> int:
    """Lista as ações do registry e, havendo mutações, o placar KEEP/DISCARD."""
    from harness.improve.target import actions

    acts = actions()
    for name in sorted(acts):
        print(name)
    muts = store.mutations(limit=None)
    if muts:
        keep = sum(1 for m in muts if m.verdict == "KEEP")
        discard = sum(1 for m in muts if m.verdict == "DISCARD")
        print(f"ações={len(acts)} mutações={len(muts)} KEEP={keep} DISCARD={discard}")
    else:
        print(f"ações={len(acts)} sem mutações no ledger")
    return 0


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
    print(
        f"provision n={len(secs)} "
        f"p50={_pct(secs, 50):.3f}s p95={_pct(secs, 95):.3f}s"
    )
    return 0


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
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    print(f"selado: {dst}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="agent harness provider-agnostic")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="executa uma unidade com um backend")
    run.add_argument("--unit", required=True, help="diretório (ou arquivo) com unit.toml")
    run.add_argument("--backend", default=None, help="obrigatório sem --route auto")
    run.add_argument("--model", default=None)
    run.add_argument("--route", choices=list(ROUTE_MODES), default=ROUTE_MANUAL,
                     help="auto: o router escolhe tier/backend/model pelo kind da "
                          "unidade e pelo histórico do ledger (exclui --backend/--model)")
    run.add_argument("--project", default=None)
    run.add_argument("--repo", default=None,
                     help="repo-alvo (git, limpo): vira o workspace e é onde os KPIs "
                          "são medidos; sem ele o run roda num tmpdir vazio")
    # default None (e não DEFAULT_MAX_TURNS): é assim que `--route auto` sabe
    # distinguir "o usuário não pediu turnos" de "o usuário pediu 8".
    run.add_argument("--max-turns", type=int, default=None, dest="max_turns")
    run.set_defaults(func=cmd_run, parser=run)

    ab = sub.add_parser("ab", help="veredito de Wilson entre dois braços")
    ab.add_argument("--a", type=_arm, metavar="SUCC/N",
                    help="braço A (baseline) já contado, ex.: 5/6")
    ab.add_argument("--b", type=_arm, metavar="SUCC/N",
                    help="braço B (candidata) já contado, ex.: 6/6")
    ab.add_argument("--min-n", type=int, default=MIN_N, dest="min_n",
                    help=f"tentativas por braço para a régua opinar (default {MIN_N})")
    # Modo de execução: o harness roda o experimento em vez de só contar.
    ab.add_argument("--dim", choices=["backend"], default=None,
                    help="dimensão testada; roda a mesma unidade nos dois braços")
    ab.add_argument("--unit", default=None, help="diretório (ou arquivo) com unit.toml")
    ab.add_argument("--a-backend", default=None, dest="a_backend")
    ab.add_argument("--b-backend", default=None, dest="b_backend")
    ab.add_argument("--a-model", default=None, dest="a_model")
    ab.add_argument("--b-model", default=None, dest="b_model")
    ab.add_argument("--n", type=int, default=MIN_N,
                    help=f"tentativas por braço (default {MIN_N})")
    ab.add_argument("--project", default=None)
    # `parser` no namespace: erro de combinação de flag sai como erro de
    # argparse (uso + exit 2), não como exceção no meio do experimento.
    ab.set_defaults(func=cmd_ab, parser=ab)

    backends = sub.add_parser("backends", help="lista backends registrados + preflight")
    backends.set_defaults(func=cmd_backends)

    improve = sub.add_parser("improve", help="ciclo de auto-melhoria: muta config e testa em A/B")
    improve.add_argument("--cycles", type=int, default=1)
    improve.add_argument("--deadline-s", type=float, default=None, dest="deadline_s",
                         help="teto de tempo do loop; estourou, escala pro humano")
    improve.add_argument("--unit", action="append", default=[],
                         help="unidade de avaliação (repetível); default: benchmarks/held_in/*")
    improve.add_argument("--backend", default=None,
                         help="fixa o executor dos DOIS braços; default: quem escolhe é o "
                              "router, lendo o config que a mutação acabou de mexer")
    improve.add_argument("--model", default=None)
    improve.add_argument("--n", type=int, default=None,
                         help="tentativas por braço (default: [improve].n_per_arm do catalog)")
    improve.add_argument("--resume", default=None, metavar="THREAD_ID",
                         help="responde a escalação pendente da thread (o id sai no "
                              "`escalate ... thread=` do stderr)")
    improve.add_argument("--answer", default=None, metavar="JSON",
                         help=f"resposta do humano ao interrupt, só com --resume "
                              f"(default: {IMPROVE_ANSWER})")
    improve.set_defaults(func=cmd_improve, parser=improve)

    replay = sub.add_parser("replay", help="atribui delta do histórico a uma mutação")
    replay.add_argument("--mutation", default=None, metavar="ID",
                        help="id da mutação (os ids saem no --list)")
    replay.add_argument("--list", action="store_true",
                        help="lista as mutações do ledger com veredito")
    replay.add_argument("--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
                        help=f"teto de linhas lidas do ledger, nos DOIS modos "
                             f"(default {DEFAULT_LIMIT})")
    replay.set_defaults(func=cmd_replay, parser=replay)

    lineage = sub.add_parser(
        "lineage", help="árvore genealógica das mutações de código"
    )
    lineage.add_argument("--file", default=None, metavar="PATH",
                         help="jsonl de linhagem (default data/lineage.jsonl)")
    lineage.add_argument("--db", default=None, metavar="PATH",
                         help="banco do ledger p/ veredito (default data/runs.sqlite)")
    lineage.add_argument("--limit", type=int, default=None, metavar="N",
                         help="mostra só as N últimas raízes")
    lineage.set_defaults(func=cmd_lineage)

    doctor = sub.add_parser(
        "doctor", help="diagnóstico local: backends, genoma, config, dados, tracing"
    )
    doctor.set_defaults(func=cmd_doctor, parser=doctor)

    skills = sub.add_parser(
        "skills", help="lista as skills carregadas (nome, kinds, descrição)"
    )
    skills.add_argument("--lift", action="store_true",
                        help="anexa o lift por skill (atribuição do ledger; "
                             "sem amostra num braço = traço)")
    skills.set_defaults(func=cmd_skills)

    actions = sub.add_parser(
        "actions", help="lista as ações do registry + KEEP/DISCARD do ledger"
    )
    actions.set_defaults(func=cmd_actions)

    seal = sub.add_parser(
        "seal", help="promove um exame da quarentena para benchmarks/sealed"
    )
    seal.add_argument("name", help="nome do dir em benchmarks/quarantine")
    seal.add_argument("--yes", action="store_true",
                      help="confirmação humana; sem isto o comando recusa")
    seal.set_defaults(func=cmd_seal)

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
