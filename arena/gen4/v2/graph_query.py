#!/usr/bin/env python3
"""graph_query.py — CLI de inspeção do store de auto-crítica (graph.py).

Uso:
    python3 graph_query.py decisions [-n 10]
    python3 graph_query.py runs <version>
    python3 graph_query.py ab <a> <b>
    python3 graph_query.py proposals
    python3 graph_query.py pending
    python3 graph_query.py confirmations [-n 20]
    python3 graph_query.py outbound [-n 20]
    python3 graph_query.py sessions [-n 10]
    python3 graph_query.py delivery <projeto> [-n 20]
    python3 graph_query.py governance [-n 20]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from graph import (  # noqa: E402
    _connect,
    delivery_history,
    pending_outbound,
    recent_confirmations,
    recent_decisions,
    recent_governance,
    recent_sessions,
    runs_for_version,
    summary_for_ab,
)


def _short(s: str, width: int = 40) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= width else s[: width - 1] + "…"


def cmd_decisions(args: argparse.Namespace) -> None:
    rows = recent_decisions(n=args.n, db_path=None)
    if not rows:
        print("(sem decisions)")
        return

    print(f"{'ts':<20} {'proposal':<12} {'outcome':<8} {'n_runs':>6}  reason")
    wins = losses = 0
    for r in rows:
        if r["outcome"] == "merge":
            wins += 1
        else:
            losses += 1
        print(
            f"{r['ts']:<20} {r['proposal_id']:<12} {r['outcome']:<8} "
            f"{r['n_runs']:>6}  {_short(r['reason'])}"
        )
    print(f"\nmerge: {wins} | discard: {losses}")


def cmd_runs(args: argparse.Namespace) -> None:
    rows = runs_for_version(args.version, db_path=None)
    if not rows:
        print(f"(sem runs para {args.version})")
        return

    print(f"{'ts':<20} {'task_id':<20} {'suite':<10} {'ok':>3} {'s':>7} {'tok':>7} {'usd':>7} v")
    for r in rows:
        print(
            f"{r['ts']:<20} {r['task_id']:<20} {r['suite']:<10} "
            f"{r['success']:>3} {r['seconds']:>7.1f} {r['tokens']:>7} "
            f"{r['cost_usd']:>7.3f} {r['valid']}"
        )
    print(f"\ntotal: {len(rows)} runs")


def _fmt_side(s: dict) -> list[str]:
    return [
        f"version:      {s['version']}",
        f"n:            {s['n']}",
        f"n_valid:      {s['n_valid']}",
        f"success_rate: {s['success_rate']:.2%}",
        f"trunc_rate:   {s['trunc_rate']:.2%}",
        f"med_s:        {s['med_s']:.1f}",
        f"cost_run:     {s['cost_run']:.4f}",
        f"tok_run:      {s['tok_run']:.1f}",
    ]


def cmd_ab(args: argparse.Namespace) -> None:
    result = summary_for_ab(args.a, args.b, db_path=None)
    a_lines = _fmt_side(result["a"])
    b_lines = _fmt_side(result["b"])
    width = max(len(l) for l in a_lines) + 4
    print(f"{'A':<{width}}B")
    for la, lb in zip(a_lines, b_lines):
        print(f"{la:<{width}}{lb}")


def cmd_proposals(args: argparse.Namespace) -> None:
    conn = _connect(None)
    try:
        rows = conn.execute(
            "SELECT * FROM proposals ORDER BY ts DESC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("(sem proposals)")
        return

    print(f"{'ts':<20} {'id':<12} {'from':<8} {'to':<8}  hypothesis")
    for r in rows:
        print(
            f"{r['ts']:<20} {r['id']:<12} {r['from_version']:<8} "
            f"{r['to_version_intended']:<8}  {_short(r['hypothesis'], 50)}"
        )
    print(f"\ntotal: {len(rows)} proposals")


def cmd_pending(args: argparse.Namespace) -> None:
    rows = pending_outbound(limit=args.n, db_path=None)
    if not rows:
        print("(sem outbound pendente)")
        return

    print(f"{'id':>4} {'ts':<20} {'to_addr':<24} {'requested_by':<12}  body")
    for r in rows:
        print(
            f"{r['id']:>4} {r['ts']:<20} {r['to_addr']:<24} {r['requested_by']:<12}  "
            f"{_short(r['body'], 50)}"
        )
    print(f"\ntotal: {len(rows)} pendentes")


def cmd_confirmations(args: argparse.Namespace) -> None:
    rows = recent_confirmations(n=args.n, db_path=None)
    if not rows:
        print("(sem confirmações/cancelamentos)")
        return

    print(f"{'ts':<20} {'outbound_id':>11} {'event':<8} {'actor':<16} {'source':<10}  to_addr / body")
    for r in rows:
        print(
            f"{r['ts']:<20} {r['outbound_id']:>11} {r['event']:<8} {r['actor']:<16} "
            f"{r['source']:<10}  {r['to_addr']} / {_short(r['outbound_body'], 30)}"
        )
    print(f"\ntotal: {len(rows)} eventos")


def cmd_outbound(args: argparse.Namespace) -> None:
    conn = _connect(None)
    try:
        rows = conn.execute(
            "SELECT * FROM outbound_messages ORDER BY ts DESC, id DESC LIMIT ?",
            (args.n,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("(sem outbound)")
        return

    print(f"{'id':>4} {'ts':<20} {'status':<9} {'to_addr':<24} {'requested_by':<12}  body")
    for r in rows:
        print(
            f"{r['id']:>4} {r['ts']:<20} {r['status']:<9} {r['to_addr']:<24} "
            f"{r['requested_by']:<12}  {_short(r['body'], 40)}"
        )
    print(f"\ntotal: {len(rows)} outbound")


def cmd_sessions(args: argparse.Namespace) -> None:
    rows = recent_sessions(n=args.n, db_path=None)
    if not rows:
        print("(sem sessions)")
        return

    print(f"{'session_id':<20} {'project':<20} {'status':<10}  updated")
    for r in rows:
        print(f"{r['session_id']:<20} {r['project']:<20} {r['status']:<10}  {r['updated']}")
    print(f"\ntotal: {len(rows)} sessions")


def cmd_delivery(args: argparse.Namespace) -> None:
    rows = delivery_history(args.project, n=args.n, db_path=None)
    if not rows:
        print(f"(sem delivery_events para {args.project})")
        return

    print(f"{'ts':<20} {'session_id':<16} {'kind':<10} {'ok':>3} {'checks':>7} {'acc':>7}  next_action")
    for r in rows:
        checks = f"{r['checks_passed']}/{r['checks_total']}"
        acc = f"{r['acceptance_passed']}/{r['acceptance_total']}"
        print(
            f"{r['ts']:<20} {r['session_id']:<16} {r['kind']:<10} "
            f"{r['delivery_success']:>3} {checks:>7} {acc:>7}  {_short(r['next_action'], 40)}"
        )
    print(f"\ntotal: {len(rows)} eventos de entrega ({args.project})")


def cmd_governance(args: argparse.Namespace) -> None:
    rows = recent_governance(n=args.n, db_path=None)
    if not rows:
        print("(sem governance_events)")
        return

    print(f"{'ts':<20} {'project':<16} {'action':<16} {'actor':<12}  detail")
    for r in rows:
        print(
            f"{r['ts']:<20} {r['project']:<16} {r['action']:<16} {r['actor']:<12}  "
            f"{_short(r['detail'], 40)}"
        )
    print(f"\ntotal: {len(rows)} eventos de governança")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspeciona o store de auto-crítica do harness.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dec = sub.add_parser("decisions", help="lista decisions recentes")
    p_dec.add_argument("-n", type=int, default=10)
    p_dec.set_defaults(func=cmd_decisions)

    p_runs = sub.add_parser("runs", help="lista runs de uma harness_version")
    p_runs.add_argument("version")
    p_runs.set_defaults(func=cmd_runs)

    p_ab = sub.add_parser("ab", help="compara duas versões lado a lado")
    p_ab.add_argument("a")
    p_ab.add_argument("b")
    p_ab.set_defaults(func=cmd_ab)

    p_props = sub.add_parser("proposals", help="lista proposals registradas")
    p_props.set_defaults(func=cmd_proposals)

    p_pending = sub.add_parser("pending", help="lista outbound pendentes de confirmação")
    p_pending.add_argument("-n", type=int, default=50)
    p_pending.set_defaults(func=cmd_pending)

    p_conf = sub.add_parser("confirmations", help="lista confirmações/cancelamentos recentes")
    p_conf.add_argument("-n", type=int, default=20)
    p_conf.set_defaults(func=cmd_confirmations)

    p_out = sub.add_parser("outbound", help="lista outbound recentes de qualquer status")
    p_out.add_argument("-n", type=int, default=20)
    p_out.set_defaults(func=cmd_outbound)

    p_sess = sub.add_parser("sessions", help="lista sessões de entrega recentes")
    p_sess.add_argument("-n", type=int, default=10)
    p_sess.set_defaults(func=cmd_sessions)

    p_deliv = sub.add_parser("delivery", help="histórico de entrega de um projeto")
    p_deliv.add_argument("project")
    p_deliv.add_argument("-n", type=int, default=20)
    p_deliv.set_defaults(func=cmd_delivery)

    p_gov = sub.add_parser("governance", help="lista eventos de governança recentes")
    p_gov.add_argument("-n", type=int, default=20)
    p_gov.set_defaults(func=cmd_governance)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
