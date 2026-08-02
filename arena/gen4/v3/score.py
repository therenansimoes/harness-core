#!/usr/bin/env python3
"""score.py — lê results.tsv e agrega por (harness_version, suite).

    python3 score.py               # resumo por versão
    python3 score.py --by-task     # abre por task (onde a falha mora)
    python3 score.py --ab v0 v0.1  # compara duas versões + regra de promoção

Não decide nada sozinho: imprime números e diz se os gates passam.

REGRA DO JUIZ (dívida paga depois do A/B v0.1):
Toda comparação A/B é NORMALIZADA por run. Somar custo entre amostras de
tamanho diferente faz o lado com menos runs parecer barato — foi assim que a
v0.1 quase foi promovida com um ganho que não existia. Runs sem telemetria
válida (truncadas pelo teto de turns, timeout, JSON quebrado) reportam $0 e
0 tokens; entrar no agregado de custo puxa a média para baixo e inventa
eficiência. Elas contam para success e para tempo, nunca para custo.
"""

from __future__ import annotations

import argparse
import os
import statistics
from collections import defaultdict
from pathlib import Path

# evolve.py roda a candidata contra o mesmo results.tsv; HARNESS_RESULTS permite
# apontar para outro log (sandbox, teste) sem duplicar lógica de score.
RESULTS = Path(os.environ.get("HARNESS_RESULTS", Path(__file__).parent / "results.tsv"))

# Marcadores de run que terminou sem telemetria confiável (ver agent.py).
BAD_TELEMETRY = ("cli_exit", "timeout", "bad_json", "max_turns")

MIN_N = 3            # runs mínimos por lado
GAIN_PCT = 0.10      # ganho mínimo para creditar melhora (10%)
IMBALANCE = 1.5      # N de um lado > 1.5x o outro = amostra desbalanceada


def load() -> list[dict]:
    if not RESULTS.exists():
        raise SystemExit("results.tsv não existe — rode run_task.py primeiro")
    lines = RESULTS.read_text().strip().splitlines()
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        # padding: a última linha do arquivo perde o \t final no strip, e sem
        # isso o zip trunca e a coluna 'notes' desaparece justo da run mais
        # recente — que é a que você está olhando.
        cells = (ln.split("\t") + [""] * len(header))[: len(header)]
        rows.append(dict(zip(header, cells)))
    return rows


def is_valid(r: dict) -> bool:
    """Telemetria confiável? (custo/tokens utilizáveis)"""
    if any(m in r.get("notes", "") for m in BAD_TELEMETRY):
        return False
    return int(r.get("tokens") or 0) > 0


def agg(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {}
    valid = [r for r in rows if is_valid(r)]
    nv = len(valid)
    succ = sum(int(r["success"]) for r in rows)
    succ_valid = sum(int(r["success"]) for r in valid)
    secs = [float(r["seconds"]) for r in rows]
    cost = sum(float(r["cost_usd"] or 0) for r in valid)
    toks = sum(int(r["tokens"] or 0) for r in valid)
    return {
        "n": n,
        "n_valid": nv,
        "n_trunc": n - nv,
        "trunc_rate": (n - nv) / n,
        "pass": succ,
        "rate": succ / n,
        # success contando só runs que terminaram limpas: se divergir do rate,
        # o success está sendo carregado por runs truncadas que "deram certo".
        "rate_valid": (succ_valid / nv) if nv else 0.0,
        "med_s": statistics.median(secs),
        # NORMALIZADO — nunca comparar somas entre amostras de tamanho diferente
        "cost_run": (cost / nv) if nv else 0.0,
        "tok_run": (toks / nv) if nv else 0.0,
        "cost_total": cost,
        "eff": (succ_valid / cost) if cost else 0.0,
    }


def fmt(label: str, a: dict) -> str:
    trunc = f"trunc {a['n_trunc']}/{a['n']}" if a["n_trunc"] else "trunc 0"
    return (
        f"{label:<18} {a['pass']:>3}/{a['n']:<3} = {a['rate']:>5.0%}  "
        f"med {a['med_s']:>5.1f}s  {a['tok_run']:>6.0f}tok/run  "
        f"${a['cost_run']:>7.4f}/run  {trunc}"
    )


def delta(a: float, b: float) -> tuple[float, str]:
    """Variação relativa de A para B. Retorna (fração, texto com sinal)."""
    if not a:
        return 0.0, "n/a"
    d = (b - a) / a
    return d, f"{d:+.1%}"


def ab_report(rows: list[dict], va: str, vb: str) -> dict:
    """Veredito estruturado — MESMA lógica de gates que o --ab imprime.

    evolve.py consome isto. Existe para que o loop de evolução use ESTE juiz e
    não invente um score paralelo: um harness com dois juízes não tem juiz.
    """
    ra = [r for r in rows if r["harness_version"] == va]
    rb = [r for r in rows if r["harness_version"] == vb]
    if not ra or not rb:
        raise SystemExit(f"faltam runs: {va}={len(ra)} {vb}={len(rb)}")
    A, B = agg(ra), agg(rb)

    d_med, _ = delta(A["med_s"], B["med_s"])
    d_cost, _ = delta(A["cost_run"], B["cost_run"])
    d_tok, _ = delta(A["tok_run"], B["tok_run"])
    faster = -d_med >= GAIN_PCT
    cheaper = -d_cost >= GAIN_PCT
    imbalanced = max(A["n_valid"], B["n_valid"]) > IMBALANCE * max(
        1, min(A["n_valid"], B["n_valid"])
    )

    gates = [
        ("success não caiu", B["rate"] >= A["rate"]),
        ("success limpo não caiu", B["rate_valid"] >= A["rate_valid"]),
        (f"N válido suficiente (>={MIN_N} por lado)", A["n_valid"] >= MIN_N and B["n_valid"] >= MIN_N),
        ("truncamento não aumentou", B["trunc_rate"] <= A["trunc_rate"]),
        (f"ganho normalizado >={GAIN_PCT:.0%} (mediana s OU custo/run)", faster or cheaper),
        ("sem regressão grave no outro eixo (<+10%)", (d_med < 0.10) and (d_cost < 0.10)),
    ]
    return {
        "version_a": va,
        "version_b": vb,
        "a": A,
        "b": B,
        "d_med": d_med,
        "d_cost": d_cost,
        "d_tok": d_tok,
        "gates": [{"name": n, "ok": bool(ok)} for n, ok in gates],
        "failed": [n for n, ok in gates if not ok],
        "imbalanced": imbalanced,
        "merge": all(ok for _, ok in gates),
    }


def do_ab(rows: list[dict], va: str, vb: str) -> int:
    rep = ab_report(rows, va, vb)
    A, B = rep["a"], rep["b"]
    d_med, d_cost = rep["d_med"], rep["d_cost"]
    s_med, s_cost = f"{d_med:+.1%}", f"{d_cost:+.1%}"
    s_tok = f"{rep['d_tok']:+.1%}"

    print(fmt(f"A {va}", A))
    print(fmt(f"B {vb}", B))
    print(f"\n  N total      A={A['n']}  B={B['n']}     (válido: A={A['n_valid']}  B={B['n_valid']})")

    print(f"  mediana s    {A['med_s']:.1f} -> {B['med_s']:.1f}   {s_med}")
    print(f"  custo/run    ${A['cost_run']:.4f} -> ${B['cost_run']:.4f}   {s_cost}")
    print(f"  tokens/run   {A['tok_run']:.0f} -> {B['tok_run']:.0f}   {s_tok}")
    print(f"  truncamento  {A['trunc_rate']:.0%} -> {B['trunc_rate']:.0%}")

    if A["rate"] != A["rate_valid"] or B["rate"] != B["rate_valid"]:
        print(
            f"  success limpo A={A['rate_valid']:.0%} B={B['rate_valid']:.0%}"
            "   <- success total inclui runs truncadas"
        )

    print()
    for g in rep["gates"]:
        print(f"  [{'PASS' if g['ok'] else 'FAIL'}] {g['name']}")

    if rep["imbalanced"]:
        print(
            f"\n  AVISO: amostra desbalanceada (A={A['n_valid']} vs B={B['n_valid']} runs válidas)."
            "\n  Métricas por run absorvem isso, mas rode N igual antes de creditar."
        )

    if rep["merge"]:
        print("\n=> MERGE candidato — confirme em sealed antes de creditar.")
    else:
        print(f"\n=> DISCARD — gate(s): {'; '.join(rep['failed'])}")
    print("   Registre a decisão em evolution/decisions/.")
    return 0 if rep["merge"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--by-task", action="store_true")
    ap.add_argument("--ab", nargs=2, metavar=("A", "B"))
    a = ap.parse_args()
    rows = load()

    if a.ab:
        return do_ab(rows, *a.ab)

    key = (
        (lambda r: (r["harness_version"], r["suite"], r["task_id"]))
        if a.by_task
        else (lambda r: (r["harness_version"], r["suite"]))
    )
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    for k in sorted(groups):
        print(fmt(" ".join(k), agg(groups[k])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
