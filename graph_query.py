#!/usr/bin/env python3
"""graph_query.py — CLI de inspeção do store de auto-crítica (graph.py).

Uso:
    python3 graph_query.py decisions [-n 10]
    python3 graph_query.py runs <version>
    python3 graph_query.py ab <a> <b>
    python3 graph_query.py proposals
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from graph import _connect, recent_decisions, runs_for_version, summary_for_ab  # noqa: E402


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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
