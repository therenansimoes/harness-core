"""CLI do harness. `harness run` / `harness ab` / `harness backends`."""

from __future__ import annotations

import argparse
import contextlib
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
from pathlib import Path

from harness.backends import registry
from harness.ledger import store
from harness.ruler.gate import Decision, gate
from harness.ruler.kpi import collect, load_kpis
from harness.ruler.verify import run_verify
from harness.ruler.wilson import MIN_N, Arm, decide_ab, wilson_interval
from harness.types import ExecRequest, ExecResult, RunRow, UnitSpec
from harness.workspace.provision import dispose, provision

UNIT_FILE = "unit.toml"
SCRATCH_DIR = ".harness"   # log do verify; não conta como sujeira do repo-alvo


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


def cmd_run(args: argparse.Namespace) -> int:
    unit = load_unit(Path(args.unit))
    backend = registry.get_backend(args.backend)
    if args.model is not None and hasattr(backend, "model"):
        # Backend model-selectable checa o modelo pedido no próprio preflight.
        backend.model = args.model

    pre = backend.preflight()
    if not pre.ok:
        print(f"preflight falhou para {args.backend}: {pre.reason}", file=sys.stderr)
        return 1

    run_id = uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    with _workspace(args.repo, run_id) as ws:
        if args.repo is None:
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
                model=args.model,
                max_turns=args.max_turns,
                trace_path=ws / "trace.jsonl",
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
        if decision.action == "revert" and args.repo is not None:
            _revert(ws)   # no tmpdir o revert é o próprio descarte

    sec_total = time.monotonic() - t0
    ok = decision.action == "accept"
    exit_reason = _exit_reason(result, decision)

    row_id = store.record_run(
        RunRow(
            run_id=run_id,
            unit_id=unit.id,
            project=args.project,
            backend=args.backend,
            model=args.model,
            tier=None,
            kind=unit.kind,
            ok=ok,
            exit_reason=exit_reason,
            sec_total=sec_total,
            sec_provision=sec_provision,
            cost_usd=result.cost_usd,
            intervention=False,
            created_at=store.now_iso(),
        )
    )
    print(
        f"{run_id} {unit.id} {args.backend} {decision.action} {decision.reason} "
        f"{sec_total:.2f}s ledger#{row_id}"
    )
    return 0 if ok else 1


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


def cmd_ab(args: argparse.Namespace) -> int:
    """Veredito de Wilson de B (candidata) contra A (baseline).

    Sai 0 em qualquer veredito: DISCARD e INCONCLUSIVE são respostas da régua,
    não erro da CLI. Quem decide o que fazer com o veredito é o chamador.
    """
    verdict = decide_ab(args.a, args.b, min_n=args.min_n)
    print(f"{verdict} a={_fmt_arm(args.a)} b={_fmt_arm(args.b)}")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="agent harness provider-agnostic")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="executa uma unidade com um backend")
    run.add_argument("--unit", required=True, help="diretório (ou arquivo) com unit.toml")
    run.add_argument("--backend", required=True)
    run.add_argument("--model", default=None)
    run.add_argument("--project", default=None)
    run.add_argument("--repo", default=None,
                     help="repo-alvo (git, limpo): vira o workspace e é onde os KPIs "
                          "são medidos; sem ele o run roda num tmpdir vazio")
    run.add_argument("--max-turns", type=int, default=8, dest="max_turns")
    run.set_defaults(func=cmd_run)

    ab = sub.add_parser("ab", help="veredito de Wilson entre dois braços")
    ab.add_argument("--a", required=True, type=_arm, metavar="SUCC/N",
                    help="braço A (baseline), ex.: 5/6")
    ab.add_argument("--b", required=True, type=_arm, metavar="SUCC/N",
                    help="braço B (candidata), ex.: 6/6")
    ab.add_argument("--min-n", type=int, default=MIN_N, dest="min_n",
                    help=f"tentativas por braço para a régua opinar (default {MIN_N})")
    ab.set_defaults(func=cmd_ab)

    backends = sub.add_parser("backends", help="lista backends registrados + preflight")
    backends.set_defaults(func=cmd_backends)

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
