#!/usr/bin/env python3
"""evolve.py — um ciclo de auto-evolução do harness, do proposal à decisão.

    python3 evolve.py --proposal evolution/proposals/dummy_discard.md
    python3 evolve.py --proposal <path> --repeat 3 --suite fixed

Fluxo:
    baseline -> proposal -> sandbox -> suite -> juiz (score.py) -> decision
    -> merge|discard -> graph

Exit: 0 = merge · 1 = discard · 2 = erro de infra (não é veredito).

Regras que este loop NÃO negocia:
- O baseline não é tocado até a decisão. A candidata vive na sandbox.
- Quem julga é o `score.py` (`ab_report`). Não existe segundo score aqui.
- Todo ciclo é gravado no graph, inclusive DISCARD — descarte também é dado.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import graph  # noqa: E402
import score  # noqa: E402

RESULTS = ROOT / "results.tsv"
SANDBOXES = ROOT / "evolution" / "sandboxes"
DECISIONS = ROOT / "evolution" / "decisions"
# O genome: o que a sandbox copia e o que um merge promove de volta.
GENOME = ["agent.py"]


class InfraError(Exception):
    """Falha de infra — exit 2. Nunca vira veredito de DISCARD."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ proposal


def parse_proposal(path: Path) -> dict:
    """Front matter TOML entre +++ ... +++, corpo markdown livre."""
    if not path.exists():
        raise InfraError(f"proposal não existe: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("+++"):
        raise InfraError(f"{path.name}: falta front matter TOML delimitado por +++")
    try:
        _, fm, body = text.split("+++", 2)
    except ValueError:
        raise InfraError(f"{path.name}: front matter não fechado com +++")
    try:
        meta = tomllib.loads(fm)
    except tomllib.TOMLDecodeError as e:
        raise InfraError(f"{path.name}: TOML inválido — {e}")

    for field in ("id", "from_version", "to_version", "hypothesis"):
        if not meta.get(field):
            raise InfraError(f"{path.name}: campo obrigatório ausente: {field}")
    change = meta.get("change")
    if not isinstance(change, dict) or not change.get("old") or not change.get("new"):
        raise InfraError(f"{path.name}: [change] precisa de 'old' e 'new'")
    change.setdefault("file", "agent.py")
    if change["file"] not in GENOME:
        raise InfraError(f"{path.name}: change.file '{change['file']}' fora do genome {GENOME}")
    meta["body"] = body.strip()
    meta["path"] = str(path)
    return meta


def apply_change(sandbox: Path, change: dict) -> str:
    """Aplica a mudança na sandbox. Exige match único — ambiguidade é erro."""
    target = sandbox / change["file"]
    src = target.read_text(encoding="utf-8")
    old, new = change["old"], change["new"]
    n = src.count(old)
    if n == 0:
        raise InfraError(f"change.old não encontrado em {change['file']} (baseline mudou?)")
    if n > 1:
        raise InfraError(f"change.old aparece {n}x em {change['file']} — precisa ser único")
    target.write_text(src.replace(old, new), encoding="utf-8")
    return f"{change['file']}: -{len(old.splitlines())} linhas / +{len(new.splitlines())} linhas"


# ------------------------------------------------------------------- sandbox


def build_sandbox(pid: str, to_version: str, change: dict) -> tuple[Path, str]:
    sandbox = SANDBOXES / pid
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    for f in GENOME + ["run_task.py"]:
        shutil.copy2(ROOT / f, sandbox / f)
    (sandbox / "harness_version.txt").write_text(to_version + "\n")
    diff_summary = apply_change(sandbox, change)
    return sandbox, diff_summary


def run_suite(sandbox: Path, suite: str, repeat: int) -> int:
    """Roda a suite com o genome da sandbox, gravando no results.tsv canônico."""
    env = {
        **os.environ,
        "HARNESS_RESULTS": str(RESULTS),   # candidata e baseline no mesmo log
        "HARNESS_TASKS_ROOT": str(ROOT),   # tasks vêm do repo, não da sandbox
    }
    proc = subprocess.run(
        [sys.executable, str(sandbox / "run_task.py"), "--all", "--suite", suite,
         "--repeat", str(repeat)],
        env=env,
        cwd=sandbox,
    )
    # returncode != 0 só significa "alguma task falhou" — isso é dado, não erro.
    return proc.returncode


# --------------------------------------------------------------------- graph


def tsv_rows() -> list[dict]:
    score.RESULTS = RESULTS
    return score.load()


def sync_graph(rows: list[dict], since_index: int, pid: str) -> int:
    """Espelha results.tsv no graph. Runs a partir de since_index são da candidata.

    Idempotente por (ts, task_id, harness_version): rodar duas vezes não duplica.
    """
    seen: set[tuple] = set()
    for v in {r["harness_version"] for r in rows}:
        for g in graph.runs_for_version(v):
            seen.add((g["ts"], g["task_id"], g["harness_version"]))

    n = 0
    for i, r in enumerate(rows):
        key = (r["timestamp"], r["task_id"], r["harness_version"])
        if key in seen:
            continue
        graph.record_run(
            task_id=r["task_id"],
            harness_version=r["harness_version"],
            suite=r["suite"],
            success=int(r["success"]),
            seconds=float(r["seconds"]),
            tokens=int(r["tokens"] or 0),
            cost_usd=float(r["cost_usd"] or 0),
            notes=r["notes"],
            valid=1 if score.is_valid(r) else 0,
            proposal_id=pid if i >= since_index else None,
            ts=r["timestamp"],
        )
        n += 1
    return n


# ------------------------------------------------------------------ decision


def write_decision(meta: dict, rep: dict, outcome: str, gid: int, diff_summary: str,
                   n_runs: int) -> Path:
    A, B = rep["a"], rep["b"]
    va, vb = rep["version_a"], rep["version_b"]
    gates_md = "\n".join(
        f"| {'PASS' if g['ok'] else 'FAIL'} | {g['name']} |" for g in rep["gates"]
    )
    if outcome == "merge":
        reason = (
            f"Todos os {len(rep['gates'])} gates passaram. Piso intacto "
            f"(success {B['rate']:.0%}, truncamento {B['trunc_rate']:.0%}) e ganho normalizado "
            f"real: custo/run {rep['d_cost']:+.1%}, mediana {rep['d_med']:+.1%}. "
            f"Genome promovido para {vb}."
        )
    else:
        reason = (
            f"Gate(s) reprovado(s): {'; '.join(rep['failed'])}. "
            f"custo/run {rep['d_cost']:+.1%}, mediana {rep['d_med']:+.1%}, "
            f"truncamento {A['trunc_rate']:.0%} -> {B['trunc_rate']:.0%}. "
            f"Baseline {va} permanece intacto."
        )
    doc = f"""# Decisão {meta['id']} — {outcome.upper()}

**proposal_id:** `{meta['id']}` · **graph_decision_id:** `{gid}`
**Proposta:** [`{Path(meta['path']).name}`](../proposals/{Path(meta['path']).name})
**Ciclo:** {va} → {vb} · {n_runs} runs de candidata · gerado por `evolve.py` em {now()}

**Hipótese:** {meta['hypothesis']}

**Mudança:** {diff_summary}

## A/B

| Métrica | A = {va} | B = {vb} | Δ |
|---|---|---|---|
| success | {A['pass']}/{A['n']} = {A['rate']:.0%} | {B['pass']}/{B['n']} = {B['rate']:.0%} | — |
| success limpo | {A['rate_valid']:.0%} | {B['rate_valid']:.0%} | — |
| truncamento | {A['trunc_rate']:.0%} | {B['trunc_rate']:.0%} | — |
| mediana s | {A['med_s']:.1f}s | {B['med_s']:.1f}s | {rep['d_med']:+.1%} |
| custo/run | ${A['cost_run']:.4f} | ${B['cost_run']:.4f} | {rep['d_cost']:+.1%} |
| tokens/run | {A['tok_run']:.0f} | {B['tok_run']:.0f} | {rep['d_tok']:+.1%} |
| N válido | {A['n_valid']} | {B['n_valid']} | — |

## Gates

| Veredito | Gate |
|---|---|
{gates_md}

{"**AVISO:** amostra desbalanceada — rode N igual antes de creditar." if rep["imbalanced"] else ""}

## Razão

{reason}

## Notas da proposta

{meta['body'] or "(sem corpo)"}
"""
    out = DECISIONS / f"{meta['id']}.md"
    out.write_text(doc, encoding="utf-8")
    return out


def promote(sandbox: Path, to_version: str) -> None:
    """MERGE: sandbox vira baseline. Só roda depois de todos os gates passarem."""
    for f in GENOME:
        shutil.copy2(sandbox / f, ROOT / f)
    (ROOT / "harness_version.txt").write_text(to_version + "\n")


# ----------------------------------------------------------------------- cli


def cycle(proposal_path: Path, repeat: int, suite: str, force: bool) -> int:
    meta = parse_proposal(proposal_path)
    pid, va, vb = meta["id"], meta["from_version"], meta["to_version"]

    current = (ROOT / "harness_version.txt").read_text().strip()
    if current != va and not force:
        raise InfraError(
            f"proposal parte de '{va}' mas o baseline está em '{current}'. "
            "Atualize from_version ou use --force."
        )
    if va == vb:
        raise InfraError("from_version e to_version são iguais — nada a comparar")

    print(f"== ciclo {pid}: {va} -> {vb} (suite={suite}, repeat={repeat})")
    sandbox, diff_summary = build_sandbox(pid, vb, meta["change"])
    print(f"   sandbox: {sandbox.relative_to(ROOT)}  [{diff_summary}]")

    graph.record_proposal(
        pid=pid, from_version=va, to_version_intended=vb,
        hypothesis=meta["hypothesis"], diff_summary=diff_summary, path=str(proposal_path),
    )

    before = len(tsv_rows())
    print(f"   rodando suite... ({repeat}x cada task)")
    run_suite(sandbox, suite, repeat)

    rows = tsv_rows()
    n_new = len(rows) - before
    if n_new <= 0:
        raise InfraError("a suite não gerou nenhuma run — nada para julgar")
    synced = sync_graph(rows, before, pid)
    print(f"   {n_new} runs de candidata · {synced} runs novas no graph")

    rep = score.ab_report(rows, va, vb)
    outcome = "merge" if rep["merge"] else "discard"

    gid = graph.record_decision(
        proposal_id=pid,
        outcome=outcome,
        scores_summary=json.dumps(
            {"a": rep["a"], "b": rep["b"], "d_med": rep["d_med"], "d_cost": rep["d_cost"]},
            ensure_ascii=False,
        ),
        reason="; ".join(rep["failed"]) if rep["failed"] else "todos os gates passaram",
        gates_json=json.dumps(rep["gates"], ensure_ascii=False),
    )

    doc = write_decision(meta, rep, outcome, gid, diff_summary, n_new)

    if outcome == "merge":
        promote(sandbox, vb)
        print(f"\n=> MERGE: genome promovido para {vb}")
    else:
        print(f"\n=> DISCARD: baseline {va} intacto — gate(s): {'; '.join(rep['failed'])}")
    print(f"   decision: {doc.relative_to(ROOT)}  (graph id {gid})")
    return 0 if outcome == "merge" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="um ciclo de auto-evolução do harness")
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--repeat", type=int, default=3, help="runs por task (default 3)")
    ap.add_argument("--suite", default="fixed")
    ap.add_argument("--force", action="store_true", help="ignora mismatch de from_version")
    a = ap.parse_args()

    DECISIONS.mkdir(parents=True, exist_ok=True)
    SANDBOXES.mkdir(parents=True, exist_ok=True)
    try:
        return cycle(Path(a.proposal).resolve(), a.repeat, a.suite, a.force)
    except InfraError as e:
        print(f"ERRO DE INFRA: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERRO DE INFRA inesperado: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
