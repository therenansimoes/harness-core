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

RÉGUA DE SUCCESS (D2, 2026-08-02): quem decide sobre success é intervalo de
Wilson, não diferença bruta de acertos nem juiz-LLM. `decide_ab` devolve
KEEP/DISCARD/INCONCLUSIVE; INCONCLUSIVE nunca promove. Com N pequeno, 5/6 vs
4/6 é ruído — a régua diz isso em vez de fingir veredito.

RÉGUA DE KPI (D4b, 2026-08-02): a coluna `kpis` do results.tsv entra no A/B por
`kpi_report` — mediana por lado e limiar de 5%, sem teste estatístico. Com o N
de uma suite não há amostra para intervalo em variável contínua; fingir um
seria pior que dizer "mediana caiu 12%". Qualquer KPI WORSE bloqueia o merge:
success igual com KPI pior é regressão, não empate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path

import kpi

# evolve.py roda a candidata contra o mesmo results.tsv; HARNESS_RESULTS permite
# apontar para outro log (sandbox, teste) sem duplicar lógica de score.
RESULTS = Path(os.environ.get("HARNESS_RESULTS", Path(__file__).parent / "results.tsv"))

# Marcadores de run que terminou sem telemetria confiável (ver agent.py).
BAD_TELEMETRY = ("cli_exit", "timeout", "bad_json", "max_turns")

MIN_N = 6            # runs mínimos por lado para a régua de Wilson opinar
MIN_N_GATE = 3       # N mínimo dos gates de eficiência (tempo/custo) do A/B
GAIN_PCT = 0.10      # ganho mínimo para creditar melhora (10%)
IMBALANCE = 1.5      # N de um lado > 1.5x o outro = amostra desbalanceada
Z = 1.96             # 95% — z do intervalo de Wilson

KPI_MIN_N = 3        # valores válidos por lado para um KPI ter veredito
KPI_FLAT_PCT = 0.05  # |delta| abaixo disso é ruído de medição, não sinal

KEEP, DISCARD, INCONCLUSIVE = "KEEP", "DISCARD", "INCONCLUSIVE"
BETTER, WORSE, FLAT = "BETTER", "WORSE", "FLAT"


def wilson_interval(successes: int, n: int, z: float = Z) -> tuple[float, float]:
    """Intervalo de Wilson (score interval) para uma proporção.

    Wald (p ± z·sqrt(p(1-p)/n)) degenera justamente no caso que aparece aqui:
    N pequeno e p colado em 0 ou 1 devolve intervalo de largura zero e finge
    certeza. Wilson não degenera — 6/6 vira [0.61, 1.0], não [1.0, 1.0].
    """
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, center - half), min(1.0, center + half)


def decide_ab(succ_a: int, n_a: int, succ_b: int, n_b: int) -> dict:
    """Veredito ternário sobre success — a régua que substituiu o juiz-LLM.

    Sobreposição de intervalos = não dá para distinguir os dois lados com o N
    que existe. INCONCLUSIVE é resposta honesta, não empate a favor de B: nunca
    promove. Quem quiser veredito, rode mais runs.
    """
    ci_a, ci_b = wilson_interval(succ_a, n_a), wilson_interval(succ_b, n_b)
    out = {
        "ci_a": ci_a,
        "ci_b": ci_b,
        "n_a": n_a,
        "n_b": n_b,
        "successes_a": succ_a,
        "successes_b": succ_b,
        "min_n": MIN_N,
        "z": Z,
    }
    if n_a < MIN_N or n_b < MIN_N:
        return {**out, "verdict": INCONCLUSIVE,
                "reason": f"N insuficiente (A={n_a} B={n_b}, mínimo {MIN_N} por lado)"}
    if ci_b[0] > ci_a[1]:
        return {**out, "verdict": KEEP,
                "reason": f"Wilson não sobrepõe e B é melhor "
                          f"(A [{ci_a[0]:.2f},{ci_a[1]:.2f}] < B [{ci_b[0]:.2f},{ci_b[1]:.2f}])"}
    if ci_a[0] > ci_b[1]:
        return {**out, "verdict": DISCARD,
                "reason": f"Wilson não sobrepõe e A é melhor "
                          f"(A [{ci_a[0]:.2f},{ci_a[1]:.2f}] > B [{ci_b[0]:.2f},{ci_b[1]:.2f}])"}
    return {**out, "verdict": INCONCLUSIVE,
            "reason": f"intervalos de Wilson se sobrepõem "
                      f"(A [{ci_a[0]:.2f},{ci_a[1]:.2f}] vs B [{ci_b[0]:.2f},{ci_b[1]:.2f}])"}


def kpi_values(rows: list[dict]) -> dict[str, list[float]]:
    """{nome: [valores válidos]} a partir da coluna `kpis` das linhas.

    Linha sem a coluna (results.tsv anterior ao D4a), com célula vazia ou com
    JSON quebrado vale {} — o A/B não pode morrer porque um log velho não tinha
    KPI. NaN é "não medido" e some da lista: entrar como 0 inventaria queda."""
    out: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        raw = (r.get("kpis") or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for name, value in data.items():
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if math.isnan(v) or math.isinf(v):
                continue
            out[str(name)].append(v)
    return dict(out)


def kpi_report(rows_a: list[dict], rows_b: list[dict],
               directions: dict[str, str] | None = None) -> dict:
    """Compara os KPIs de dois lados: mediana + limiar, e um veredito por KPI.

    Mediana (não média) porque uma run truncada distorce a média de um N de
    suite. Sem intervalo de confiança: com 6 valores por lado em variável
    contínua, qualquer CI seria decoração. O limiar de 5% é o que separa
    "mediu diferente" de "mudou".

    KPI presente em um lado só é ignorado (não há comparação a fazer, e tratar
    ausência como zero fabricaria regressão). Menos de KPI_MIN_N valores
    válidos em qualquer lado => FLAT com reason de n — a comparação aparece no
    relatório, mas não move o gate.

    `directions` vem do kpi.toml do alvo (kpi.load_directions); nome ausente =
    "up" (maior é melhor).
    """
    directions = directions or {}
    va, vb = kpi_values(rows_a), kpi_values(rows_b)

    report: dict[str, dict] = {}
    for name in sorted(set(va) & set(vb)):
        xs, ys = va[name], vb[name]
        direction = directions.get(name, kpi.DEFAULT_DIRECTION)
        med_a, med_b = statistics.median(xs), statistics.median(ys)
        entry = {
            "name": name,
            "n_a": len(xs),
            "n_b": len(ys),
            "median_a": med_a,
            "median_b": med_b,
            "direction": direction,
            "delta": None,
            "verdict": FLAT,
            "reason": "",
        }
        if len(xs) < KPI_MIN_N or len(ys) < KPI_MIN_N:
            entry["reason"] = (f"valores válidos insuficientes "
                               f"(A={len(xs)} B={len(ys)}, mínimo {KPI_MIN_N} por lado)")
            report[name] = entry
            continue
        if med_a == 0:
            entry["reason"] = "mediana A = 0 — delta relativo indefinido"
            report[name] = entry
            continue

        d = (med_b - med_a) / abs(med_a)
        entry["delta"] = d
        if abs(d) < KPI_FLAT_PCT:
            entry["reason"] = f"variação {d:+.1%} abaixo do limiar de {KPI_FLAT_PCT:.0%}"
        else:
            improved = d > 0 if direction == kpi.UP else d < 0
            entry["verdict"] = BETTER if improved else WORSE
            entry["reason"] = (f"mediana {med_a:g} -> {med_b:g} ({d:+.1%}, "
                               f"direction={direction})")
        report[name] = entry

    worse = [n for n, e in report.items() if e["verdict"] == WORSE]
    return {
        "kpis": report,
        "worse": worse,
        # regressão de KPI bloqueia promoção mesmo com success igual (D4b).
        "blocked": bool(worse),
        "only_a": sorted(set(va) - set(vb)),
        "only_b": sorted(set(vb) - set(va)),
        "min_n": KPI_MIN_N,
        "flat_pct": KPI_FLAT_PCT,
    }


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


def ab_report(rows: list[dict], va: str, vb: str,
              directions: dict[str, str] | None = None) -> dict:
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

    # Régua de Wilson sobre success (D2): aqui ela entra como não-regressão —
    # DISCARD é regressão estatisticamente distinguível e barra o merge. KEEP
    # em success não dispensa os gates de eficiência, e INCONCLUSIVE (o caso
    # normal com N de suite) não credita nada sozinho.
    verdict = decide_ab(A["pass"], A["n"], B["pass"], B["n"])

    # KPI do alvo (D4b): entra como gate de não-regressão. Um KPI WORSE barra o
    # merge sozinho — o harness pode ficar igual em success e ter piorado o que
    # o projeto mede.
    kpis = kpi_report(ra, rb, directions)
    kpi_gate = ("sem regressão de KPI"
                + (f" (WORSE: {', '.join(kpis['worse'])})" if kpis["worse"] else ""))

    gates = [
        ("success não caiu", B["rate"] >= A["rate"]),
        ("success limpo não caiu", B["rate_valid"] >= A["rate_valid"]),
        (f"Wilson não acusa regressão de success ({verdict['verdict']})",
         verdict["verdict"] != DISCARD),
        (f"N válido suficiente (>={MIN_N_GATE} por lado)",
         A["n_valid"] >= MIN_N_GATE and B["n_valid"] >= MIN_N_GATE),
        ("truncamento não aumentou", B["trunc_rate"] <= A["trunc_rate"]),
        (f"ganho normalizado >={GAIN_PCT:.0%} (mediana s OU custo/run)", faster or cheaper),
        ("sem regressão grave no outro eixo (<+10%)", (d_med < 0.10) and (d_cost < 0.10)),
        (kpi_gate, not kpis["blocked"]),
    ]
    return {
        "version_a": va,
        "version_b": vb,
        "a": A,
        "b": B,
        "d_med": d_med,
        "d_cost": d_cost,
        "d_tok": d_tok,
        "verdict": verdict["verdict"],
        "wilson": verdict,
        "kpi": kpis,
        "gates": [{"name": n, "ok": bool(ok)} for n, ok in gates],
        "failed": [n for n, ok in gates if not ok],
        "imbalanced": imbalanced,
        "merge": all(ok for _, ok in gates),
    }


def do_ab(rows: list[dict], va: str, vb: str,
          directions: dict[str, str] | None = None) -> int:
    rep = ab_report(rows, va, vb, directions)
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
    w = rep["wilson"]
    print(f"  Wilson 95%   A [{w['ci_a'][0]:.2f},{w['ci_a'][1]:.2f}] "
          f"B [{w['ci_b'][0]:.2f},{w['ci_b'][1]:.2f}]   -> {w['verdict']} ({w['reason']})")

    k = rep["kpi"]
    if k["kpis"] or k["only_a"] or k["only_b"]:
        print("\n  KPI (mediana A -> B, limiar "
              f"{k['flat_pct']:.0%}, mínimo {k['min_n']} valores/lado)")
        for e in k["kpis"].values():
            d = f"{e['delta']:+.1%}" if e["delta"] is not None else "n/a"
            print(f"    {e['name']:<18} {e['median_a']:>10.4g} -> {e['median_b']:<10.4g} "
                  f"{d:>7}  n={e['n_a']}/{e['n_b']}  [{e['verdict']}] {e['reason']}")
        for side, names in (("só em A", k["only_a"]), ("só em B", k["only_b"])):
            if names:
                print(f"    ignorados ({side}): {', '.join(names)}")

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
        # direction dos KPIs: kpi.toml do alvo. HARNESS_KPI_ROOT aponta pro
        # repo medido quando ele não é o diretório do results.tsv; ausente =>
        # {} e todo KPI vale "up" (maior é melhor).
        directions = kpi.load_directions(
            os.environ.get("HARNESS_KPI_ROOT", RESULTS.parent)
        )
        return do_ab(rows, *a.ab, directions=directions)

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
