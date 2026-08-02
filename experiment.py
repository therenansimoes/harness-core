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
    decisão, régua de Wilson do score.py aplicada).

O runner NUNCA edita o genoma real (agent.py na raiz) nem promove/rejeita —
só escreve o draft. Quem decide é o humano/orquestrador, à mão ou via
evolve.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import score  # noqa: E402

EXPERIMENTS_DIR = ROOT / "evolution" / "experiments"

# Mesmo schema do results.tsv de run_task.py (header congelado, ver comentário
# em project.py) — hardcoded aqui em vez de importado para não puxar agent.py
# (e suas dependências de backend) só para ler uma lista de colunas.
RESULTS_HEADER = [
    "timestamp", "harness_version", "backend", "model", "suite",
    "task_id", "success", "seconds", "tokens", "cost_usd", "turns", "notes",
]


# intervalo do laço de poll do pool de runs paralelas.
POLL_INTERVAL_S = 1.0


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
    # legado da régua anterior (diferença bruta de sucessos): mantido para os
    # TOMLs e JSONs já gravados continuarem legíveis. Quem decide é o Wilson.
    cfg["decision_rule"].setdefault("min_diff_successes", 2)
    cfg.setdefault("parallel", False)
    # custo estimado por run, usado SÓ no modo parallel para pré-calcular um
    # teto nominal de n_per_arm antes de disparar (não dá pra parar no meio
    # quando tudo sobe de uma vez via Popen — ver run_experiment).
    cfg.setdefault("est_cost_per_run", 0.35)
    # teto de runs simultâneas no modo parallel — disparar tudo de uma vez
    # esbarra em rate limit da conta e mata o experimento.
    cfg.setdefault("max_workers", 6)
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


def collect_result(results_path: Path, origin: str) -> dict:
    """Lê a última linha de um results.tsv (formato HEADER de run_task.py) e
    converte os campos numéricos. `origin` só entra na mensagem de erro."""
    if not results_path.exists():
        raise ExperimentError(f"run_task.py não gravou results.tsv em {origin}")
    lines = results_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise ExperimentError(f"results.tsv em {origin} sem linha de dado após a run")
    header = lines[0].split("\t")
    values = lines[-1].split("\t")
    row = dict(zip(header, values))
    row["success"] = int(row.get("success") or 0)
    row["cost_usd"] = float(row.get("cost_usd") or 0.0)
    row["tokens"] = int(row.get("tokens") or 0)
    row["turns"] = int(row.get("turns") or 0)
    return row


def run_task_once(worktree: Path, task: str) -> dict:
    """Roda UMA run de `task` via run_task.py do worktree e devolve a última
    linha gravada em <worktree>/results.tsv (o run_task.py daquele worktree
    grava lá por padrão, um results.tsv por braço). Modo sequencial: as runs
    nunca coexistem no mesmo worktree, então o results.tsv compartilhado do
    braço não corre risco de colisão de append."""
    subprocess.run(
        [sys.executable, str(worktree / "run_task.py"), task],
        cwd=worktree, capture_output=True, text=True,
    )
    return collect_result(worktree / "results.tsv", str(worktree))


def run_task_launch(worktree: Path, task: str, run_id: str, results_path: Path) -> subprocess.Popen:
    """Dispara (sem esperar) UMA run de `task` via run_task.py do worktree —
    usado só no modo parallel. HARNESS_RESULTS aponta pra um arquivo EXCLUSIVO
    desta run (run_task.py já respeita essa env, ver ROOT/run_task.py) — é
    assim que evitamos runs paralelas colidindo no append do mesmo
    results.tsv, sem precisar de lock de arquivo. HARNESS_RUN_ID identifica a
    run pro trace (agent.py já lê essa env para nomear o trace)."""
    env = dict(os.environ)
    env["HARNESS_RESULTS"] = str(results_path)
    env["HARNESS_RUN_ID"] = run_id
    return subprocess.Popen(
        [sys.executable, str(worktree / "run_task.py"), task],
        cwd=worktree, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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


# Régua de score.py -> vocabulário do draft. INCONCLUSIVE nunca promove: é o
# mesmo efeito prático de rejeitar, mas o artefato registra o rótulo certo —
# "não deu para distinguir" não é "a mutação é ruim".
_OUTCOME = {
    score.KEEP: "promover",
    score.DISCARD: "rejeitar",
    score.INCONCLUSIVE: "inconclusivo",
}


def decide(agg: dict, decision_rule: dict) -> dict:
    """Veredito do experimento — quem decide é a régua de Wilson do score.py.

    O runner não tem juiz próprio (nem LLM, nem diferença bruta de sucessos):
    um harness com dois juízes não tem juiz. `decision_rule` sobrevive só como
    metadado do TOML (min_diff_successes é legado da régua anterior).
    """
    w = score.decide_ab(
        agg["A"]["successes"], agg["A"]["n"],
        agg["B"]["successes"], agg["B"]["n"],
    )
    return {
        "outcome": _OUTCOME[w["verdict"]],
        "verdict": w["verdict"],
        "reason": w["reason"],
        "rule": "wilson",
        "min_n": w["min_n"],
        "ci_a": list(w["ci_a"]),
        "ci_b": list(w["ci_b"]),
        "diff_successes": agg["B"]["successes"] - agg["A"]["successes"],
    }


# --------------------------------------------------------------- orquestra


def run_experiment(cfg: dict, parallel: bool | None = None) -> dict:
    """`parallel=None` (default) usa o campo `parallel` do TOML; True/False
    força o modo, é o que a flag --parallel da CLI usa."""
    use_parallel = cfg.get("parallel", False) if parallel is None else parallel
    if use_parallel:
        return _run_experiment_parallel(cfg)

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
        "mode": "sequential",
    }


def _run_experiment_parallel(cfg: dict) -> dict:
    """Modo parallel: as runs de A e B sobem via Popen num pool de no máximo
    max_workers simultâneas, na ordem intercalada A0, B0, A1, B1, ... — A e B
    contemporâneos por construção (melhor garantia que intercalar). Sem jeito
    de parar no meio quando tudo já foi disparado, então o budget vira um
    teto NOMINAL calculado ANTES de disparar (n_per_arm x 2 x
    est_cost_per_run); se estourar, n_per_arm é reduzido ali, nunca depois."""
    name = cfg["name"]
    task = cfg["task"]
    n_requested = cfg["n_per_arm"]
    budget = cfg["budget_usd"]
    mutation = cfg["mutation"]
    est_cost = cfg.get("est_cost_per_run", 0.35)
    max_workers = max(1, int(cfg.get("max_workers", 6)))

    n_per_arm = n_requested
    budget_capped = False
    if n_requested * 2 * est_cost > budget:
        n_per_arm = int(budget // (2 * est_cost))
        budget_capped = True
        if n_per_arm < 1:
            raise ExperimentError(
                f"budget_usd={budget} insuficiente p/ 1 par ao custo estimado "
                f"est_cost_per_run={est_cost} (mínimo ${2 * est_cost:.2f}/par)"
            )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(tempfile.mkdtemp(prefix=f"experiment_{name}_"))
    wt_a, wt_b = base / "A", base / "B"
    results_dir = Path(tempfile.mkdtemp(prefix=f"experiment_{name}_results_"))

    runs: list[dict] = []
    mutation_summary = ""
    try:
        create_worktree(wt_a)
        create_worktree(wt_b)
        mutation_summary = apply_mutation(wt_b, mutation)  # só B recebe a mutação
        run_setup(wt_a, task)
        run_setup(wt_b, task)

        # fila na ordem intercalada A0, B0, A1, B1, ... — cada run com seu
        # próprio HARNESS_RESULTS, então nada colide no append. Dispara no
        # máximo max_workers de uma vez (rate limit da conta): a cada run
        # concluída, a próxima da fila sobe.
        pending = [
            (arm, i, wt)
            for i in range(n_per_arm)
            for arm, wt in (("A", wt_a), ("B", wt_b))
        ]
        next_idx = 0
        active: list[tuple] = []
        while next_idx < len(pending) or active:
            while next_idx < len(pending) and len(active) < max_workers:
                arm, i, wt = pending[next_idx]
                run_id = f"{name}_{arm}_{i}_{ts}"
                results_path = results_dir / f"{run_id}.tsv"
                proc = run_task_launch(wt, task, run_id, results_path)
                active.append((proc, arm, i, results_path))
                next_idx += 1

            still_running = []
            for entry in active:
                proc, arm, i, results_path = entry
                if proc.poll() is None:
                    still_running.append(entry)
                    continue
                proc.wait()
                row = collect_result(results_path, str(results_path))
                row["arm"] = arm
                row["pair_index"] = i
                runs.append(row)
            progressed = len(still_running) != len(active)
            active = still_running
            if active and not progressed:
                time.sleep(POLL_INTERVAL_S)
    finally:
        remove_worktree(wt_a)
        remove_worktree(wt_b)
        shutil.rmtree(results_dir, ignore_errors=True)

    runs.sort(key=lambda r: (r["pair_index"], r["arm"]))
    total_cost = sum(r["cost_usd"] for r in runs)
    agg = aggregate(runs)
    decision = decide(agg, cfg["decision_rule"])
    return {
        "name": name,
        "task": task,
        "mutation": mutation,
        "mutation_summary": mutation_summary,
        "n_per_arm_requested": n_requested,
        "n_per_arm_effective": n_per_arm,
        "budget_usd": budget,
        "budget_capped": budget_capped,
        "est_cost_per_run": est_cost,
        "decision_rule": cfg["decision_rule"],
        "timestamp": ts,
        "runs": runs,
        "aggregates": agg,
        "cost_total_usd": total_cost,
        "stopped_early": False,
        "stopped_at_pair": None,
        "decision": decision,
        "mode": "parallel",
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
    if result.get("budget_capped"):
        print(f"\nn_per_arm reduzido de {result['n_per_arm_requested']} para {result['n_per_arm_effective']} "
              f"ANTES de disparar — budget nominal (2 x n x est_cost_per_run) excedia "
              f"${result['budget_usd']:.4f} (est_cost_per_run=${result['est_cost_per_run']:.2f})")
    d = result["decision"]
    print(f"\nWilson 95%  A [{d['ci_a'][0]:.2f},{d['ci_a'][1]:.2f}]  B [{d['ci_b'][0]:.2f},{d['ci_b'][1]:.2f}] "
          f"· diff de sucessos (B-A): {d['diff_successes']} -> {d['outcome'].upper()}")
    print(f"  {d['reason']}")


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
    if result.get("budget_capped"):
        lines.append(
            f"- Modo parallel: n_per_arm reduzido de {result['n_per_arm_requested']} para "
            f"{result['n_per_arm_effective']} ANTES de disparar (teto nominal pré-calculado, "
            f"est_cost_per_run=${result['est_cost_per_run']:.2f}/run) — não dá pra parar no meio "
            f"com todas as runs em paralelo."
        )
    lines += [
        "",
        "## Regra de decisão",
        "",
        f"Régua de Wilson 95% (N mínimo {d['min_n']} por braço): "
        f"A [{d['ci_a'][0]:.2f}, {d['ci_a'][1]:.2f}] vs B [{d['ci_b'][0]:.2f}, {d['ci_b'][1]:.2f}], "
        f"diff de sucessos (B-A) = {d['diff_successes']} -> **{d['outcome'].upper()}** "
        f"({d['verdict']}) — {d['reason']}.",
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
    run_p.add_argument(
        "--parallel", action="store_true",
        help="força modo parallel (todas as runs via Popen simultâneo), mesmo sem `parallel = true` no TOML",
    )
    args = ap.parse_args()

    if args.cmd == "run":
        try:
            cfg = parse_experiment(Path(args.config))
            result = run_experiment(cfg, parallel=True if args.parallel else None)
        except ExperimentError as e:
            print(f"ERRO DE INFRA: {e}", file=sys.stderr)
            return 2
        print_table(result)
        json_path, draft_path = write_outputs(result)
        print(f"\n{json_path}\n{draft_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
