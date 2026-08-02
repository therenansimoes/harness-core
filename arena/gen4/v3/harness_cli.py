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

    python3 harness_cli.py project add <nome> --path <abs> [--priority N]
    python3 harness_cli.py project list
    python3 harness_cli.py project queue <nome> add "<título>" --prompt f.md --verify v.py
    python3 harness_cli.py project run [--project N] [--once|--loop K] [--keep]
    python3 harness_cli.py project status [<nome>]

    python3 harness_cli.py project-init <nome> [--path DIR] [--ui]
    python3 harness_cli.py session-new --project X --session Y [--brief-file FILE]
    python3 harness_cli.py verify --project X --session Y
    python3 harness_cli.py post-work --project X --session Y
    python3 harness_cli.py resume --project X --session Y
    python3 harness_cli.py promote-checks --project X --session Y
    python3 harness_cli.py governance-approve --project X [--note "..."]

Não existe subcomando que envie mensagem direto (nem "whatsapp-send"). O
único caminho de envio é whatsapp-confirm sobre um pending que já existe —
isso é o gate, não um detalhe de implementação.

Eixo de ENTREGA (project-init, session-new, verify, post-work, resume,
promote-checks, governance-approve) é casca fina sobre delivery.py: toda a
lógica de verify/governança/post-work vive lá, não aqui.

`project` (add/list/queue/run/status) é o eixo MULTI-PROJETO (SPEC-MULTIPROJECT
FASE 1): vários projetos com work_path fora do repo, fila própria, lock
próprio, results.tsv próprio — delega inteiro para project.py via subprocess
(mesmo padrão de run/evolve), independente do eixo project-init/session-new
acima (que é single-project, formato demo_site).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
import delivery  # noqa: E402
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


def cmd_project(a: argparse.Namespace) -> int:
    import subprocess

    return subprocess.run([sys.executable, str(ROOT / "project.py"), *a.args]).returncode


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


# --------------------------------------------------------------------------- delivery


def cmd_project_init(a: argparse.Namespace) -> int:
    path = Path(a.path or f"projects/{a.name}").resolve()
    delivery.init_project(path, a.name, ui=a.ui)
    print(f"projeto criado em {path}")
    for p in sorted(path.rglob("*")):
        print(f"  {p.relative_to(path)}")
    return 0


def cmd_session_new(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    brief = ""
    if a.brief_file:
        brief = Path(a.brief_file).read_text(encoding="utf-8")
    session_dir = delivery.new_session(project, a.session, brief=brief)
    print(f"sessão criada em {session_dir}")
    return 0


def _print_layer(titulo: str, rows: list[dict]) -> None:
    print(f"-- {titulo} --")
    if not rows:
        print("  (nenhum check)")
        return
    for r in rows:
        status = "PASS" if r["ok"] else "FAIL"
        motivo = f" — {r['reason']}" if r["reason"] else ""
        print(f"  [{status}] {r['name']}{motivo}")


def cmd_verify(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    v = delivery.verify(project, a.session)
    _print_layer("regression", v["regression"])
    _print_layer("acceptance", v["acceptance"])
    print(f"regression: {v['regression_passed']}/{v['regression_total']}  "
          f"acceptance: {v['acceptance_passed']}/{v['acceptance_total']}  "
          f"total: {v['checks_passed']}/{v['checks_total']}")
    if v["governance_violations"]:
        print("!! VIOLAÇÃO DE GOVERNANÇA !!")
        for viol in v["governance_violations"]:
            print(f"  - {viol}")
    if v["new_unregistered_checks"]:
        print(f"checks novos não registrados: {', '.join(v['new_unregistered_checks'])}")
    print(f"delivery_success: {v['delivery_success']}")
    return 0 if v["delivery_success"] == 1 else 1


def cmd_post_work(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    v = delivery.post_work(project, a.session, actor="cli")
    _print_layer("regression", v["regression"])
    _print_layer("acceptance", v["acceptance"])
    print(f"regression: {v['regression_passed']}/{v['regression_total']}  "
          f"acceptance: {v['acceptance_passed']}/{v['acceptance_total']}  "
          f"total: {v['checks_passed']}/{v['checks_total']}")
    if v["governance_violations"]:
        print("!! VIOLAÇÃO DE GOVERNANÇA !!")
        for viol in v["governance_violations"]:
            print(f"  - {viol}")
    if v["open_issues"]:
        print("issues em aberto:")
        for issue in v["open_issues"]:
            print(f"  - {issue}")
    print(f"next_action: {v['next_action']}")
    print(f"report: {v['report']}")
    if v["proposal_stub"]:
        print(f"proposal_stub: {v['proposal_stub']}")
    print(f"delivery_success: {v['delivery_success']}")
    return 0 if v["delivery_success"] == 1 else 1


def cmd_resume(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    print(delivery.resume(project, a.session))
    return 0


def cmd_promote_checks(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    movidos = delivery.promote_checks(project, a.session, actor="cli")
    if not movidos:
        print("nada a promover")
    else:
        print(f"promovidos {len(movidos)} checks para regression:")
        for m in movidos:
            print(f"  - {m}")
    return 0


def cmd_governance_approve(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    detail = a.note or "aprovação manual"
    man = delivery.write_manifest(project, actor="cli", detail=detail)
    print(f"MANIFEST reescrito: {len(man['checks'])} checks registrados")
    return 0


def cmd_ui_test(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    result = delivery.run_ui_suite(project)
    if not result["ran"]:
        print(f"UI não rodou: {result['reason']}")
        return 2
    for test in result["tests"]:
        status = "PASS" if test["ok"] else "FAIL"
        motivo = f" — {test['reason']}" if test["reason"] else ""
        print(f"[{status}] {test['name']}{motivo}")
    print(f"ui: {result['passed']}/{result['total']}")
    return 0 if result["passed"] == result["total"] else 1


def cmd_ui_baseline(a: argparse.Namespace) -> int:
    project = delivery.resolve_project(a.project)
    result = delivery.run_ui_suite(project, update_baseline=True)
    if not result["ran"]:
        print(f"UI não rodou: {result['reason']}")
        return 2
    project_name = project.name
    detail = a.note or "atualização manual de baseline"
    graph.record_governance_event(project=project_name, action="update_ui_baseline",
                                   actor="cli", detail=detail)
    print(f"Baseline atualizada: {result['total']} checks rodaram")
    print("!! AVISO: baseline nova passa a ser a verdade. Se ela foi gravada com um bug visual na tela,")
    print("   o bug vira o esperado e nenhum check volta a reclamar sobre isso.")
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

    p_pinit = sub.add_parser(
        "project-init", help="cria a árvore de um projeto de ENTREGA (projects/<nome>)"
    )
    p_pinit.add_argument("name", help="nome do projeto")
    p_pinit.add_argument("--path", default=None, help="diretório do projeto (default: projects/<nome>)")
    p_pinit.add_argument("--ui", action="store_true", help="projeto tem critérios de UI (revisão humana)")
    p_pinit.set_defaults(func=cmd_project_init)

    p_snew = sub.add_parser("session-new", help="abre uma nova sessão de entrega num projeto")
    p_snew.add_argument("--project", required=True, help="nome (projects/<nome>) ou path do projeto")
    p_snew.add_argument("--session", required=True, help="id da sessão")
    p_snew.add_argument("--brief-file", default=None, help="arquivo com o brief da sessão")
    p_snew.set_defaults(func=cmd_session_new)

    p_verify = sub.add_parser(
        "verify", help="roda regression + acceptance da sessão e avalia governança"
    )
    p_verify.add_argument("--project", required=True)
    p_verify.add_argument("--session", required=True)
    p_verify.set_defaults(func=cmd_verify)

    p_pw = sub.add_parser(
        "post-work", help="verify + decide next_action + gera report da sessão"
    )
    p_pw.add_argument("--project", required=True)
    p_pw.add_argument("--session", required=True)
    p_pw.set_defaults(func=cmd_post_work)

    p_resume = sub.add_parser("resume", help="resumo formatado do estado da sessão")
    p_resume.add_argument("--project", required=True)
    p_resume.add_argument("--session", required=True)
    p_resume.set_defaults(func=cmd_resume)

    p_promote = sub.add_parser(
        "promote-checks", help="promove acceptance/<session> aprovado para regression/ permanente"
    )
    p_promote.add_argument("--project", required=True)
    p_promote.add_argument("--session", required=True)
    p_promote.set_defaults(func=cmd_promote_checks)

    p_gov = sub.add_parser(
        "governance-approve",
        help="ÚNICO comando que reescreve o MANIFEST de regression. Uso: aprovação "
             "manual e explícita do dono após remover/editar um check de regression. "
             "Nunca é chamado implicitamente por verify/post-work — isso destruiria "
             "o gate de governança.",
    )
    p_gov.add_argument("--project", required=True)
    p_gov.add_argument("--note", default="", help="motivo da aprovação (registrado no manifest)")
    p_gov.set_defaults(func=cmd_governance_approve)

    p_uit = sub.add_parser("ui-test", help="roda suite Playwright e exibe placar")
    p_uit.add_argument("--project", required=True, help="nome (projects/<nome>) ou path do projeto")
    p_uit.set_defaults(func=cmd_ui_test)

    p_uib = sub.add_parser(
        "ui-baseline",
        help="roda suite Playwright com --update-snapshots e registra o ato na governança. "
             "SENSÍVEL: baseline nova passa a ser a verdade — se gravada com bug visual, "
             "o bug vira esperado e nenhum check reclamará.",
    )
    p_uib.add_argument("--project", required=True, help="nome (projects/<nome>) ou path do projeto")
    p_uib.add_argument("--note", default="", help="motivo da atualização (registrado na governança)")
    p_uib.set_defaults(func=cmd_ui_baseline)

    p_project = sub.add_parser(
        "project",
        help="eixo multi-projeto: add/list/queue/run/status (delega para project.py, "
             "SPEC-MULTIPROJECT FASE 1)",
    )
    p_project.add_argument("args", nargs=argparse.REMAINDER, help="subcomando e args de project.py")
    p_project.set_defaults(func=cmd_project)

    return ap


def main() -> int:
    ap = build_parser()
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
