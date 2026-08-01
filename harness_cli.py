#!/usr/bin/env python3
"""harness_cli.py — casca fina sobre os módulos do harness. Não reimplementa
lógica: importa e chama, ou delega via subprocess para preservar saída ao
vivo e o exit code real de run_task.py / evolve.py.

    python3 harness_cli.py run [--all] [--suite fixed|sealed] [--repeat N] [task]
    python3 harness_cli.py evolve --proposal <path> [--repeat N] [--no-credit] [--force]
    python3 harness_cli.py status
    python3 harness_cli.py whatsapp-pending
    python3 harness_cli.py whatsapp-confirm <id> [--note ...]
    python3 harness_cli.py whatsapp-cancel <id> [--note ...]
    python3 harness_cli.py init [--path DIR]

Não existe subcomando que envie mensagem direto (nem "whatsapp-send"). O
único caminho de envio é whatsapp-confirm sobre um pending que já existe —
isso é o gate, não um detalhe de implementação.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import graph  # noqa: E402
import score  # noqa: E402
import whatsapp  # noqa: E402

ROOT = Path(__file__).parent.resolve()


def harness_version() -> str:
    f = ROOT / "harness_version.txt"
    return f.read_text().strip() if f.exists() else "v0"


# --------------------------------------------------------------------- run/evolve


def cmd_run(a: argparse.Namespace) -> int:
    import subprocess

    cmd = [sys.executable, str(ROOT / "run_task.py")]
    if a.all:
        cmd.append("--all")
    cmd += ["--suite", a.suite, "--repeat", str(a.repeat)]
    if a.keep:
        cmd.append("--keep")
    if a.task:
        cmd.append(a.task)
    return subprocess.run(cmd).returncode


def cmd_evolve(a: argparse.Namespace) -> int:
    import subprocess

    cmd = [
        sys.executable, str(ROOT / "evolve.py"),
        "--proposal", a.proposal,
        "--repeat", str(a.repeat),
        "--suite", a.suite,
    ]
    if a.force:
        cmd.append("--force")
    if a.no_credit:
        cmd.append("--no-credit")
    return subprocess.run(cmd).returncode


# --------------------------------------------------------------------------- status


def cmd_status(a: argparse.Namespace) -> int:
    v = harness_version()
    print(f"versão atual: {v}")

    try:
        rows = score.load()
        by_version = [r for r in rows if r["harness_version"] == v]
        if by_version:
            agg = score.agg(by_version)
            print(f"score ({v}): {agg['pass']}/{agg['n']} = {agg['rate']:.0%}  "
                  f"med {agg['med_s']:.1f}s  ${agg['cost_run']:.4f}/run")
        else:
            print(f"score ({v}): sem runs registradas")
    except SystemExit as e:
        print(f"score: {e}")

    decisions = graph.recent_decisions(1)
    if decisions:
        d = decisions[0]
        print(f"última decision: {d.get('outcome')}  proposal={d.get('proposal_id')}  ts={d.get('ts')}")
    else:
        print("última decision: nenhuma")

    n_pending = len(whatsapp.pending())
    print(f"whatsapp pendentes: {n_pending}")

    st = whatsapp.service_status()
    if st.get("connected"):
        print(f"whatsapp service: online  jid={st.get('jid')}")
    else:
        print(f"whatsapp service: offline ({st.get('last_error', 'sem detalhe')})")

    return 0


# --------------------------------------------------------------------------- whatsapp


def cmd_whatsapp_pending(a: argparse.Namespace) -> int:
    rows = whatsapp.pending()
    if not rows:
        print("nenhum pendente")
        return 0
    for r in rows:
        trecho = (r.get("body") or "")[:60].replace("\n", " ")
        print(f"[{r['id']}] {r.get('ts')}  to={r.get('to_addr')}  "
              f"by={r.get('requested_by')}  {trecho!r}")
    return 0


def cmd_whatsapp_confirm(a: argparse.Namespace) -> int:
    try:
        row = whatsapp.confirm_send(a.id, actor="cli", source="cli")
    except whatsapp.NotAllowed as e:
        print(f"recusado: {e}", file=sys.stderr)
        return 1
    except whatsapp.ServiceError as e:
        print(f"erro de serviço: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    print(f"enviado: id={row.get('id')} message_id={row.get('message_id')}")
    return 0


def cmd_whatsapp_cancel(a: argparse.Namespace) -> int:
    try:
        row = whatsapp.cancel_send(a.id, actor="cli", source="cli", note=a.note or "")
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    print(f"cancelado: id={row.get('id')}")
    return 0


# --------------------------------------------------------------------------- init


def cmd_init(a: argparse.Namespace) -> int:
    target = Path(a.path).resolve()
    harness_dir = target / ".harness"
    if harness_dir.exists():
        print(f"{harness_dir} já existe — nada a fazer")
        return 0

    harness_dir.mkdir(parents=True)
    (harness_dir / "harness_version").write_text(harness_version() + "\n")
    (harness_dir / "config.toml").write_text(
        "# config local deste projeto — sobrepõe ~/.config/harness-core/config.toml\n"
        "# descomente e ajuste conforme necessário:\n\n"
        + "\n".join(f"# {line}" if line.strip() else line
                     for line in config.example_toml().splitlines())
        + "\n"
    )
    results_dir = harness_dir / "results"
    results_dir.mkdir()
    (results_dir / ".gitkeep").touch()

    print(f"criado {harness_dir}")
    return 0


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="harness_cli.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="roda a suite (delega para run_task.py)")
    p_run.add_argument("task", nargs="?", help="pasta da task (ex: tasks/task_01)")
    p_run.add_argument("--all", action="store_true")
    p_run.add_argument("--suite", default="fixed", help="fixed | sealed")
    p_run.add_argument("--repeat", type=int, default=1)
    p_run.add_argument("--keep", action="store_true", help="não apagar o workspace")
    p_run.set_defaults(func=cmd_run)

    p_evolve = sub.add_parser("evolve", help="um ciclo de auto-evolução (delega para evolve.py)")
    p_evolve.add_argument("--proposal", required=True)
    p_evolve.add_argument("--repeat", type=int, default=3)
    p_evolve.add_argument("--suite", default="fixed")
    p_evolve.add_argument("--force", action="store_true", help="ignora mismatch de from_version")
    p_evolve.add_argument("--no-credit", action="store_true",
                           help="pula a confirmação em sealed")
    p_evolve.set_defaults(func=cmd_evolve)

    p_status = sub.add_parser("status", help="resumo curto: versão, score, decision, outbound")
    p_status.set_defaults(func=cmd_status)

    p_wp = sub.add_parser(
        "whatsapp-pending",
        help="lista pendentes de outbound. NÃO existe whatsapp-send: o único "
             "caminho de envio é confirmar um pending com whatsapp-confirm.",
    )
    p_wp.set_defaults(func=cmd_whatsapp_pending)

    p_wc = sub.add_parser(
        "whatsapp-confirm",
        help="confirma e ENVIA um pending existente (a única porta de saída do gate)",
    )
    p_wc.add_argument("id", type=int)
    p_wc.add_argument("--note", default="")
    p_wc.set_defaults(func=cmd_whatsapp_confirm)

    p_wx = sub.add_parser("whatsapp-cancel", help="cancela um pending sem enviar")
    p_wx.add_argument("id", type=int)
    p_wx.add_argument("--note", default="")
    p_wx.set_defaults(func=cmd_whatsapp_cancel)

    p_init = sub.add_parser("init", help="cria .harness/ (versão pinada, config, results/) no projeto")
    p_init.add_argument("--path", default=".", help="diretório do projeto (default: cwd)")
    p_init.set_defaults(func=cmd_init)

    return ap


def main() -> int:
    ap = build_parser()
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
