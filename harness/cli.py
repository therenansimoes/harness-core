"""CLI do harness. `harness run` / `harness backends`."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from harness.backends import registry
from harness.ledger import store
from harness.types import ExecRequest, RunRow, UnitSpec

UNIT_FILE = "unit.toml"


def _bootstrap() -> None:
    """Telemetria de terceiro é opt-in explícito, nunca default."""
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")


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


def _verify(unit: UnitSpec, ws: Path, timeout_s: float = 120.0) -> bool:
    # Provisório: `ruler/verify.py` assume isto no PR-4 e devolve um Verdict.
    proc = subprocess.run(
        unit.verify_cmd, shell=True, cwd=ws, capture_output=True, timeout=timeout_s
    )
    return proc.returncode == 0


def cmd_run(args: argparse.Namespace) -> int:
    unit = load_unit(Path(args.unit))
    backend = registry.get_backend(args.backend)

    pre = backend.preflight()
    if not pre.ok:
        print(f"preflight falhou para {args.backend}: {pre.reason}", file=sys.stderr)
        return 1

    run_id = uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"harness-{run_id}-") as tmp:
        ws = Path(tmp)
        sec_provision = time.monotonic() - t0
        result = backend.execute(
            ExecRequest(
                prompt=unit.prompt,
                workspace=ws,
                model=args.model,
                max_turns=args.max_turns,
                trace_path=ws / "trace.jsonl",
            )
        )
        passed = _verify(unit, ws) if result.ok else False

    sec_total = time.monotonic() - t0
    ok = result.ok and passed
    if not result.ok:
        exit_reason = result.exit_reason
    else:
        exit_reason = "done" if passed else "verify_failed"

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
    print(f"{run_id} {unit.id} {args.backend} {exit_reason} {sec_total:.2f}s ledger#{row_id}")
    return 0 if ok else 1


def cmd_backends(args: argparse.Namespace) -> int:
    for name in registry.available():
        try:
            pre = registry.get_backend(name).preflight()
            status = "ok" if pre.ok else "indisponível"
            print(f"{name:<16} {status:<14} {pre.reason}")
        except Exception as exc:  # backend quebrado não derruba a listagem
            print(f"{name:<16} {'erro':<14} {exc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="agent harness provider-agnostic")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="executa uma unidade com um backend")
    run.add_argument("--unit", required=True, help="diretório (ou arquivo) com unit.toml")
    run.add_argument("--backend", required=True)
    run.add_argument("--model", default=None)
    run.add_argument("--project", default=None)
    run.add_argument("--max-turns", type=int, default=8, dest="max_turns")
    run.set_defaults(func=cmd_run)

    backends = sub.add_parser("backends", help="lista backends registrados + preflight")
    backends.set_defaults(func=cmd_backends)

    return p


def main(argv: list[str] | None = None) -> int:
    _bootstrap()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
