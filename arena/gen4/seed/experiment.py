#!/usr/bin/env python3
"""experiment.py — runner automático de experimentos A/B (worktrees descartáveis).

    python3 experiment.py run evolution/experiments_def/exp.toml

Esta sessão fez os A/Bs de v0.3 a v0.5 (ver evolution/decisions/) na mão:
criar worktree, aplicar mutação, intercalar A/B, coletar do results.tsv —
tudo repetido a cada rodada. Este runner automatiza esse protocolo.

Lição de v0.3 (evolution/decisions/v0.3.md), que este runner existe para não
deixar ninguém esquecer de novo: "Comparação de custo/tokens ENTRE DIAS é
inválida... Todo A/B de custo precisa de braços contemporâneos." Por isso os
runs aqui são sempre intercalados A,B,A,B,... — nunca todo o braço A primeiro
e o B depois — para que nenhuma diferença de dia/cache/nondeterminismo vaze
para dentro do delta medido.

Fluxo:
    parse exp.toml/json -> worktree A (HEAD) + worktree B (HEAD + mutação,
    old->new igual ao formato de change do evolve.py) -> setup.sh de cada um
    (se existir) -> intercala n_per_arm runs por braço via run_task.py de
    cada worktree, parando no par completo em que o custo acumulado >= budget
    -> agrega -> grava evolution/experiments/<name>_<ts>.json (runs crus +
    agregados) e evolution/experiments/<name>_<ts>-draft.md (DRAFT de
    decisão, regra min_diff_successes aplicada).

O runner NUNCA edita o genoma real (agent.py na raiz) nem promove/rejeita —
só escreve o draft. Quem decide é o humano/orquestrador, à mão ou via
evolve.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
EXPERIMENTS_DIR = ROOT / "evolution" / "experiments"

# Mesmo schema do results.tsv de run_task.py (header congelado, ver comentário
# em project.py) — hardcoded aqui em vez de importado para não puxar agent.py
# (e suas dependências de backend) só para ler uma lista de colunas.
RESULTS_HEADER = [
    "timestamp", "harness_version", "backend", "model", "suite",
    "task_id", "success", "seconds", "tokens", "cost_usd", "turns", "notes",
]


class ExperimentError(Exception):
    """Erro de definição/infra do experimento — não é veredito de A/B."""


# ------------------------------------------------------------------ parsing


def parse_experiment(path: Path) -> dict:
    if not path.exists():
        raise ExperimentError(f"experimento não existe: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        cfg = json.loads(text)
    else:
        try:
            cfg = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ExperimentError(f"{path.name}: TOML inválido — {e}")

    for field in ("name", "mutation", "task", "n_per_arm", "budget_usd"):
        if field not in cfg:
            raise ExperimentError(f"{path.name}: campo obrigatório ausente: {field}")

    mutation = cfg["mutation"]
    if not isinstance(mutation, dict) or not mutation.get("old") or not mutation.get("new"):
        raise ExperimentError(f"{path.name}: [mutation] precisa de 'old' e 'new'")
    mutation.setdefault("file", "agent.py")

    cfg.setdefault("decision_rule", {})
    cfg["decision_rule"].setdefault("min_diff_successes", 2)
    return cfg


# ---------------------------------------------------------------- worktrees


def create_worktree(dest: Path) -> None:
    """git worktree add --detach <dest> HEAD — cópia descartável do HEAD atual."""
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(dest), "HEAD"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )


def remove_worktree(dest: Path) -> None:
    """Remove o worktree; tolerante — chamado no finally, nunca deve derrubar
    o resto da limpeza se o worktree nem chegou a ser criado."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=ROOT, capture_output=True, text=True,
    )
    shutil.rmtree(dest, ignore_errors=True)


def apply_mutation(worktree: Path, mutation: dict) -> str:
    """Aplica a mutação no worktree B. Mesmo formato/contrato de change do
    evolve.py: exige que 'old' exista e seja único em 'file'."""
    target = worktree / mutation["file"]
    src = target.read_text(encoding="utf-8")
    old, new = mutation["old"], mutation["new"]
    n = src.count(old)
    if n == 0:
        raise ExperimentError(f"mutation.old não encontrado em {mutation['file']}")
    if n > 1:
        raise ExperimentError(f"mutation.old aparece {n}x em {mutation['file']} — precisa ser único")
    target.write_text(src.replace(old, new), encoding="utf-8")
    return f"{mutation['file']}: -{len(old.splitlines())} linhas / +{len(new.splitlines())} linhas"


def run_setup(worktree: Path, task: str) -> None:
    setup = worktree / task / "setup.sh"
    if setup.exists():
        subprocess.run(["bash", str(setup)], cwd=worktree / task, check=True,
                        capture_output=True, text=True)


# -------------------------------------------------------------------- runs


def run_task_once(worktree: Path, task: str) -> dict:
    """Roda UMA run de `task` via run_task.py do worktree e devolve a última
    linha gravada em <worktree>/results.tsv (o run_task.py daquele worktree
    grava lá por padrão, um results.tsv por braço)."""
    subprocess.run(
        [sys.executable, str(worktree / "run_task.py"), task],
        cwd=worktree, capture_output=True, text=True,
    )
    results_path = worktree / "results.tsv"
    if not results_path.exists():
        raise ExperimentError(f"run_task.py não gravou results.tsv em {worktree}")
    lines = results_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise ExperimentError(f"results.tsv em {worktree} sem linha de dado após a run")
    header = lines[0].split("\t")
    values = lines[-1].split("\t")
    row = dict(zip(header, values))
    row["success"] = int(row.get("success") or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0.0)
    row["tokens"] = int(row.get("tokens") or 0)
    row["turns"] = int(row.get("turns") or 0)
    return row


def aggregate(runs: list[dict]) -> dict:
    agg: dict[str, dict] = {}
    for arm in ("A", "B"):
        arm_runs = [r for r in runs if r["arm"] == arm]
        n = len(arm_runs)
        successes = sum(r["success"] for r in arm_runs)
        agg[arm] = {
            "n": n,
            "successes": successes,
            "success_rate": successes / n if n else 0.0,
            "cost_usd_total": sum(r["cost_usd"] for r in arm_runs),
            "cost_usd_avg": (sum(r["cost_usd"] for r in arm_runs) / n) if n else 0.0,
            "tokens_avg": (sum(r["tokens"] for r in arm_runs) / n) if n else 0.0,
            "turns_avg": (sum(r["turns"] for r in arm_runs) / n) if n else 0.0,
        }
    return agg


def decide(agg: dict, decision_rule: dict) -> dict:
    min_diff = decision_rule.get("min_diff_successes", 2)
    diff = agg["B"]["successes"] - agg["A"]["successes"]
    if diff >= min_diff:
        outcome = "promover"
    elif -diff >= min_diff:
        outcome = "rejeitar"
    else:
        outcome = "inconclusivo"
    return {"outcome": outcome, "diff_successes": diff, "min_diff_successes": min_diff}


# --------------------------------------------------------------- orquestra


def run_experiment(cfg: dict) -> dict:
    name = cfg["name"]
    task = cfg["task"]
    n_per_arm = cfg["n_per_arm"]
    budget = cfg["budget_usd"]
    mutation = cfg["mutation"]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(tempfile.mkdtemp(prefix=f"experiment_{name}_"))
    wt_a, wt_b = base / "A", base / "B"

    runs: list[dict] = []
    total_cost = 0.0
    stopped_at_pair: int | None = None
    mutation_summary = ""
    try:
        create_worktree(wt_a)
        create_worktree(wt_b)
        mutation_summary = apply_mutation(wt_b, mutation)  # só B recebe a mutação
        run_setup(wt_a, task)
        run_setup(wt_b, task)

        for i in range(n_per_arm):
            for arm, wt in (("A", wt_a), ("B", wt_b)):  # sempre A,B — nunca o braço inteiro de um lado
                row = run_task_once(wt, task)
                row["arm"] = arm
                row["pair_index"] = i
                runs.append(row)
                total_cost += row["cost_usd"]
            if total_cost >= budget:
                stopped_at_pair = i
                break
    finally:
        remove_worktree(wt_a)
        remove_worktree(wt_b)

    agg = aggregate(runs)
    decision = decide(agg, cfg["decision_rule"])
    return {
        "name": name,
        "task": task,
        "mutation": mutation,
        "mutation_summary": mutation_summary,
        "n_per_arm_requested": n_per_arm,
        "budget_usd": budget,
        "decision_rule": cfg["decision_rule"],
        "timestamp": ts,
        "runs": runs,
        "aggregates": agg,
        "cost_total_usd": total_cost,
        "stopped_early": stopped_at_pair is not None,
        "stopped_at_pair": stopped_at_pair,
        "decision": decision,
    }


# -------------------------------------------------------------------- I/O


def print_table(result: dict) -> None:
    agg = result["aggregates"]
    print(f"\nexperimento: {result['name']}  task: {result['task']}")
    print(f"{'braço':<6}{'n':<5}{'sucessos':<10}{'taxa':<8}{'custo_total':<14}{'custo_médio':<14}"
          f"{'tokens_méd':<12}{'turns_méd':<10}")
    for arm in ("A", "B"):
        a = agg[arm]
        print(f"{arm:<6}{a['n']:<5}{a['successes']:<10}{a['success_rate']:<8.0%}"
              f"${a['cost_usd_total']:<13.4f}${a['cost_usd_avg']:<13.4f}"
              f"{a['tokens_avg']:<12.0f}{a['turns_avg']:<10.1f}")
    if result["stopped_early"]:
        print(f"\nPAROU no par {result['stopped_at_pair']} — custo acumulado ${result['cost_total_usd']:.4f} "
              f">= budget ${result['budget_usd']:.4f}")
    d = result["decision"]
    print(f"\ndiff de sucessos (B-A): {d['diff_successes']} · min_diff_successes={d['min_diff_successes']} "
          f"-> {d['outcome'].upper()}")


def render_draft(result: dict) -> str:
    agg, d = result["aggregates"], result["decision"]
    lines = [
        f"# Draft — {result['name']} ({result['timestamp']}) — {d['outcome'].upper()}",
        "",
        f"**Task:** `{result['task']}` · **Mutação:** {result['mutation_summary']} "
        f"(`{result['mutation']['file']}`)",
        "",
        "## Evidência (A/B contemporâneo intercalado)",
        "",
        f"- Braço A: {agg['A']['successes']}/{agg['A']['n']} sucesso — "
        f"custo médio ${agg['A']['cost_usd_avg']:.4f}/run.",
        f"- Braço B: {agg['B']['successes']}/{agg['B']['n']} sucesso — "
        f"custo médio ${agg['B']['cost_usd_avg']:.4f}/run.",
        f"- Custo total do experimento: ${result['cost_total_usd']:.4f} (teto ${result['budget_usd']:.4f}).",
    ]
    if result["stopped_early"]:
        lines.append(f"- Parou no par {result['stopped_at_pair']} por teto de custo.")
    lines += [
        "",
        "## Regra de decisão",
        "",
        f"diff de sucessos (B-A) = {d['diff_successes']}, "
        f"min_diff_successes = {d['min_diff_successes']} -> **{d['outcome'].upper()}**.",
        "",
        "## Draft — NÃO é decisão final",
        "",
        "Este arquivo é gerado por experiment.py. O runner nunca edita o genoma "
        "real nem promove/rejeita sozinho — humano/orquestrador decide, "
        "eventualmente via evolve.py.",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_outputs(result: dict) -> tuple[Path, Path]:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{result['name']}_{result['timestamp']}"
    json_path = EXPERIMENTS_DIR / f"{stem}.json"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    draft_path = EXPERIMENTS_DIR / f"{stem}-draft.md"
    draft_path.write_text(render_draft(result), encoding="utf-8")
    return json_path, draft_path


# -------------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="roda um experimento a partir de um .toml/.json")
    run_p.add_argument("config", help="ex: evolution/experiments_def/exp.toml")
    args = ap.parse_args()

    if args.cmd == "run":
        try:
            cfg = parse_experiment(Path(args.config))
            result = run_experiment(cfg)
        except ExperimentError as e:
            print(f"ERRO DE INFRA: {e}", file=sys.stderr)
            return 2
        print_table(result)
        json_path, draft_path = write_outputs(result)
        print(f"\n{json_path}\n{draft_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
