#!/usr/bin/env python3
"""run_judge.py — orquestra uma avaliação de juiz (FASE 1 do SPEC-J1).

Generalizado para os 3 juízes do registry (j_b2b, j_web, j_hw) — nada
hardcoded por juiz além da convenção `benchmarks/judge/task_<judge_id>` e
da linha em `judges/registry.tsv`.

Fluxo real (§6 do SPEC-J1), por juiz:
    1. fixtures/ do task_<judge_id> já provisionadas (setup.sh — chamado se
       faltar).
    2. run_task.py roda o agente na task, com --keep (workspace preservado).
    3. verify.py roda de novo, direto, contra o workspace preservado, pra
       extrair a linha estruturada `JUDGE_RESULT=...` (D1/D3 granular) que
       run_task.py descarta (results.tsv só guarda os últimos 160 chars).
       task_j_web/verify.py e task_j_hw/verify.py têm parsers próprios
       (parse_bnt_counts/parse_test_counts) por baixo — run_judge só
       depende da linha `JUDGE_RESULT=...` e do exit code, não do formato
       de pytest/bnt/ctest.
    4. persona.py entrega P1/P2 com citação.
    5. verdict.json gravado em judges/verdicts/<judge_id>/<harness_version>.json.

--dry-run: pula (2)-(4), usa números sintéticos + persona em modo mock
(PERSONA_MOCK=1), só para validar o formato do verdict (§7 do SPEC-J1).
Este harness ainda não faz nenhuma chamada paga — o caminho real acima
existe mas só foi exercitado com --dry-run / PERSONA_MOCK=1 até aqui.

--all-judges roda os 3 em sequência (real ou --dry-run) e agrega score por
juiz + mediana + spread num summary em
judges/verdicts/summary_<harness_version>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import persona  # noqa: E402

DEFAULT_JUDGE_ID = "j_b2b"
REGISTRY = REPO_ROOT / "judges" / "registry.tsv"
VERDICTS_DIR = REPO_ROOT / "judges" / "verdicts"
RESULTS = REPO_ROOT / "results.tsv"
SPREAD_INCONCLUSIVE_THRESHOLD = 25


def task_dir_for(judge_id: str) -> Path:
    return REPO_ROOT / "benchmarks" / "judge" / f"task_{judge_id}"


def all_judge_ids() -> list[str]:
    """Ordem dos judge_id como aparecem em registry.tsv, restrito à régua
    J1 (FASE 1 — os juízes que este run_judge.py sabe orquestrar via
    task_<judge_id>/verify.py + persona). Registry pode ganhar entradas de
    outras trilhas/réguas (ex.: SPEC-J2) que não seguem essa convenção —
    nenhuma lista de judge_id hardcoded, só o filtro pela régua."""
    lines = REGISTRY.read_text().splitlines()
    header = lines[0].split("\t")
    idx = header.index("judge_id")
    rv_idx = header.index("rubric_version")
    return [
        cols[idx]
        for line in lines[1:]
        if line.strip()
        for cols in [line.split("\t")]
        if cols[rv_idx] == "J1"
    ]

# pesos da régua v1 — espelha judges/RUBRIC-J1.md, fase 1 (D1-D4 + P1 + P2).
WEIGHTS = {"D1": 25, "D2": 15, "D3": 10, "D4": 10, "P1": 15, "P2": 10}


def harness_version() -> str:
    f = REPO_ROOT / "harness_version.txt"
    return f.read_text().strip() if f.exists() else "v0"


def read_registry_row(judge_id: str) -> dict:
    lines = REGISTRY.read_text().splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        cols = line.split("\t")
        row = dict(zip(header, cols))
        if row.get("judge_id") == judge_id:
            return row
    raise SystemExit(f"{judge_id} não encontrado em {REGISTRY}")


# ---------------------------------------------------------------- D1-D4


def synthetic_deterministic() -> dict:
    """Números sintéticos pro --dry-run — não reflete run nenhuma real."""
    return {
        "D1": WEIGHTS["D1"],
        "D2": WEIGHTS["D2"],
        "D3": WEIGHTS["D3"],
        "D4": WEIGHTS["D4"],
        "veto": False,
        "evidence": {
            "target_test": "passed (sintético --dry-run)",
            "full_suite": "414 passed, 0 failed (sintético --dry-run)",
            "cost_usd": 0.42,
            "turns": 4,
        },
    }


def compute_deterministic(
    verify_result: dict, tampered: bool, cost_usd: float, turns: int, task_id: str = "task_j_b2b"
) -> dict:
    """Traduz a saída estruturada do verify.py (JUDGE_RESULT=...) + o
    tamper-check do run_task.py em D1-D4, seguindo RUBRIC-J1."""
    d1 = WEIGHTS["D1"] if verify_result.get("target_ok") else 0

    veto = bool(tampered)
    d2 = 0 if veto else WEIGHTS["D2"]

    full = verify_result.get("full", {}) or {}
    total = full.get("total", 0)
    regressions = full.get("failed", 0) + full.get("errors", 0)
    d3 = (
        round(WEIGHTS["D3"] * (1 - regressions / total))
        if total > 0
        else 0
    )
    d3 = max(0, d3)

    d4 = WEIGHTS["D4"]  # sem baseline histórico -> default cheio (RUBRIC-J1 §D4)
    baseline = median_baseline_cost_turns(task_id)
    if baseline is not None:
        med_cost, med_turns = baseline
        ratio = max(cost_usd / med_cost if med_cost else 1.0, turns / med_turns if med_turns else 1.0)
        d4 = max(0, round(WEIGHTS["D4"] * max(0.0, 1 - max(0.0, ratio - 1))))

    return {
        "D1": d1,
        "D2": d2,
        "D3": d3,
        "D4": d4,
        "veto": veto,
        "evidence": {
            "target_test": "passed" if verify_result.get("target_ok") else "failed",
            "full_suite": f"{full.get('passed', 0)} passed, {regressions} regressions / {total} total",
            "cost_usd": cost_usd,
            "turns": turns,
        },
    }


def median_baseline_cost_turns(task_id: str = "task_j_b2b") -> tuple[float, float] | None:
    """Mediana de cost_usd/turns de runs anteriores do mesmo judge_id em
    results.tsv (suite=judge). None se não houver histórico ainda."""
    if not RESULTS.exists():
        return None
    rows = []
    with RESULTS.open() as fh:
        header = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            row = dict(zip(header, cols))
            if row.get("suite") == "judge" and row.get("task_id") == task_id:
                rows.append(row)
    if not rows:
        return None
    costs = sorted(float(r["cost_usd"]) for r in rows if r.get("cost_usd"))
    turns = sorted(int(r["turns"]) for r in rows if r.get("turns"))
    if not costs or not turns:
        return None
    mid_c, mid_t = len(costs) // 2, len(turns) // 2
    return costs[mid_c], turns[mid_t]


# ------------------------------------------------------------ P1/P2 e ficha


def _normalize_for_match(s: str) -> str:
    """trace.jsonl guarda a linha bruta do stream (JSON ainda serializado
    — `\\n` dentro do campo `text` é 2 chars literais, não quebra de
    linha). A persona lê essa mesma linha renderizada, mas quando ela cita
    um trecho no `quote` do JSON de resposta, esse JSON passa por
    json.loads() e qualquer `\\n`/`\\t` vira caractere real de novo. Sem
    normalizar os dois lados pro mesmo formato, uma citação legítima que
    atravessa esse ponto vira falso positivo de 'não sustentada'. Também
    colapsa espaços múltiplos (variação de formatação do modelo)."""
    s = s.replace("\\n", " ").replace("\\t", " ").replace("\n", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", s).strip()


def validate_and_score_persona(ficha: dict, diff: str, trace: str) -> tuple[dict, list[str], bool, str]:
    """Aplica a regra de citação do RUBRIC-J1: sem citação -> descartado
    (sai do cálculo); citação cujo `quote` não aparece no material
    correspondente (diff pra P1, trace pra P2) -> **veto de persona**:
    P1 e P2 saem do cálculo (viram discarded), mas D1-D4 NÃO são afetados
    — isso é diferente do veto de candidato (D2, trapaça), que zera a
    ficha inteira. Quem decide o que fazer com o veto de persona é
    build_verdict (via o retorno `persona_vetoed`)."""
    material = {"P1": _normalize_for_match(diff), "P2": _normalize_for_match(trace)}
    scored, discarded = {}, []
    for key in ("P1", "P2"):
        entry = ficha.get(key) or {}
        citation = (entry.get("citation") or "").strip()
        quote = (entry.get("quote") or "").strip()
        if not citation:
            discarded.append(key)
            continue
        if not quote or _normalize_for_match(quote) not in material[key]:
            reason = f"persona vetada: citação inválida em {key}: {citation!r} não sustentada pelo material"
            return {}, ["P1", "P2"], True, reason
        scored[key] = {
            "score": int(entry.get("score", 0)),
            "citation": citation,
            "quote": quote,
        }
    return scored, discarded, False, ""


# ------------------------------------------------------------------- verdict


def build_verdict(
    judge_id: str,
    reg: dict,
    deterministic: dict,
    persona_scored: dict,
    discarded: list[str],
    persona_vetoed: bool,
    veto_reason: str,
    cost_usd: float,
) -> dict:
    """`persona_vetoed` (citação inválida em P1/P2) só descarta P1/P2 — os
    determinísticos D1-D4 seguem valendo e entram no cálculo normal (o
    denominador já desconta discarded/vetoed). `deterministic["veto"]`
    (D2 — trapaça do candidato: tamper/segredo/escrita fora) é a ÚNICA
    fonte de veto TOTAL (judge_score = 0), conforme RUBRIC-J1 §veto."""
    candidate_veto = bool(deterministic.get("veto", False))
    if candidate_veto and not veto_reason:
        veto_reason = "D2: tamper/segredo/escrita fora do workspace"

    if candidate_veto:
        judge_score = 0
    else:
        numer = sum(deterministic[k] for k in ("D1", "D2", "D3", "D4"))
        denom = sum(WEIGHTS[k] for k in ("D1", "D2", "D3", "D4"))
        for key, entry in persona_scored.items():
            numer += entry["score"]
            denom += WEIGHTS[key]
        judge_score = round(numer / denom * 100) if denom > 0 else 0

    return {
        "judge_id": judge_id,
        "harness_version": harness_version(),
        "rubric_version": reg["rubric_version"],
        "base_sha": reg["base_sha"],
        "sealed_sha256": reg["sealed_sha256"],
        "deterministic": deterministic,
        "persona": persona_scored,
        "discarded": discarded,
        "persona_vetoed": persona_vetoed,
        "veto_reason": veto_reason,
        "judge_score": judge_score,
        "cost_usd": cost_usd,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_infra_error_verdict(judge_id: str, reg: dict, row: dict | None) -> dict:
    """Curto-circuito quando o agente consumiu 0 tokens: a run nem chegou a
    trabalhar (ex.: `claude -p` saiu antes de emitir JSON parseável — infra
    quebrada, não a run de fato tentando e falhando). Sem trabalho do
    agente não há o que julgar: persona nunca é chamada, D1-D4 não são
    calculados, e judge_score fica None — 0 significa "trabalho ruim",
    não "infra quebrada", e não pode ser confundido com ele."""
    notes = row.get("notes", "") if row else "(sem linha em results.tsv)"
    return {
        "judge_id": judge_id,
        "harness_version": harness_version(),
        "rubric_version": reg["rubric_version"],
        "base_sha": reg["base_sha"],
        "sealed_sha256": reg["sealed_sha256"],
        "infra_error": True,
        "infra_error_reason": f"agente consumiu 0 tokens — notes={notes!r}",
        "deterministic": None,
        "persona": {},
        "discarded": [],
        "persona_vetoed": False,
        "veto_reason": "",
        "judge_score": None,
        "cost_usd": 0.0,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_verdict(verdict: dict) -> Path:
    out_dir = VERDICTS_DIR / verdict["judge_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{verdict['harness_version']}.json"
    out.write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n")
    return out


# --------------------------------------------------------------- caminho real


def load_trace(notes: str) -> str:
    """Lê runs/<run_id>/trace.jsonl a partir do token `trace:<path>` gravado
    por run_task.py nas notes (SPEC-J2 design 1) e renderiza `"{i}: {line}"`
    — a persona só acerta `trace.jsonl:N` se enxergar o N na frente da
    linha. Sem token ou arquivo ausente (ex.: backend `api`, que ainda não
    grava trace): cai no comportamento antigo, as notes cruas."""
    m = re.search(r"trace:(\S+)", notes or "")
    if not m:
        return notes or ""
    trace_path = REPO_ROOT / m.group(1)
    if not trace_path.exists():
        return notes or ""
    lines = trace_path.read_text().splitlines()
    return "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=1))


def run_real(judge_id: str = DEFAULT_JUDGE_ID) -> dict:
    """Roda run_task.py de verdade (agente real via `claude -p`, custo
    real) e monta a ficha a partir do resultado. Não exercitado neste PR
    (proibido chamar API paga) — implementado, não testado end-to-end."""
    task_dir = task_dir_for(judge_id)
    task_id = f"task_{judge_id}"

    if not task_dir.joinpath("fixtures", "pyproject.toml").exists():
        subprocess.run(["bash", str(task_dir / "setup.sh")], check=True)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run_task.py"), str(task_dir), "--suite", "judge", "--keep"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=700,
    )
    print(proc.stdout)
    m = re.search(r"workspace:\s*(\S+)", proc.stdout)
    if not m:
        raise SystemExit(f"run_judge: não achei o workspace na saída de run_task.py:\n{proc.stdout}\n{proc.stderr}")
    ws = Path(m.group(1))

    diff_proc = subprocess.run(
        ["diff", "-ru", str(task_dir / "fixtures"), str(ws), "--exclude=.venv", "--exclude=venv"],
        capture_output=True,
        text=True,
    )
    diff = diff_proc.stdout

    verify_proc = subprocess.run(
        [sys.executable, str(task_dir / "verify.py")], cwd=ws, capture_output=True, text=True, timeout=120
    )
    verify_out = verify_proc.stdout + verify_proc.stderr
    jm = re.search(r"JUDGE_RESULT=(\{.*\})", verify_out)
    verify_result = json.loads(jm.group(1)) if jm else {"target_ok": False, "full": {}}

    row = last_results_row(task_id)
    tokens = int(row["tokens"]) if row and row.get("tokens", "").strip() else 0
    if tokens == 0:
        # infra quebrou antes do agente trabalhar (0 tokens = nenhuma
        # chamada ao modelo aconteceu) — curto-circuita: sem persona, sem
        # D1-D4, judge_score fica None em vez de um 0 que pareceria "o
        # agente tentou e foi mal".
        shutil.rmtree(ws, ignore_errors=True)
        reg = read_registry_row(judge_id)
        return build_infra_error_verdict(judge_id, reg, row)

    cost_usd = float(row["cost_usd"]) if row else 0.0
    turns = int(row["turns"]) if row else 0
    # run_task.py grava "tamper:test_file_modified" nas notes quando o
    # agente editou test_*.py em vez de resolver — é o único sinal de
    # tamper que ele expõe pra fora hoje.
    tampered = bool(row and row.get("notes", "").startswith("tamper:"))

    trace = load_trace(row.get("notes", "") if row else "")

    deterministic = compute_deterministic(verify_result, tampered, cost_usd, turns, task_id)
    ficha = persona.call_persona(deterministic, diff, trace, verify_out)
    persona_scored, discarded, persona_vetoed, veto_reason = validate_and_score_persona(ficha, diff, trace)

    shutil.rmtree(ws, ignore_errors=True)

    reg = read_registry_row(judge_id)
    return build_verdict(
        judge_id, reg, deterministic, persona_scored, discarded, persona_vetoed, veto_reason, cost_usd
    )


def last_results_row(task_id: str) -> dict | None:
    if not RESULTS.exists():
        return None
    header = None
    last = None
    with RESULTS.open() as fh:
        for i, line in enumerate(fh):
            cols = line.rstrip("\n").split("\t")
            if i == 0:
                header = cols
                continue
            row = dict(zip(header, cols))
            if row.get("task_id") == task_id:
                last = row
    return last


def run_dry(judge_id: str = DEFAULT_JUDGE_ID) -> dict:
    reg = read_registry_row(judge_id)
    deterministic = synthetic_deterministic()
    os.environ.setdefault("PERSONA_MOCK", "1")
    diff = "schwifty/checksum/germany.py:1\n-        return checksum\n+        return super().reconcile(checksum)\n"
    trace = "trace.jsonl:1 DONE: corrigido Algorithm11.reconcile para delegar ao método base\n"
    ficha = persona.call_persona(deterministic, diff, trace, "78 passed\n414 passed")
    persona_scored, discarded, persona_vetoed, veto_reason = validate_and_score_persona(ficha, diff, trace)
    return build_verdict(
        judge_id, reg, deterministic, persona_scored, discarded, persona_vetoed, veto_reason,
        deterministic["evidence"]["cost_usd"],
    )


# ------------------------------------------------------------------ --all-judges


def run_one(judge_id: str, dry_run: bool) -> dict:
    verdict = run_dry(judge_id) if dry_run else run_real(judge_id)
    out = write_verdict(verdict)
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    print(f"\nverdict gravado em {out}")
    print(f"judge_score = {verdict['judge_score']}")
    return verdict


def build_summary(scores: dict[str, int | None]) -> dict:
    """Agrega score por juiz: mediana + spread (max-min) sobre os scores
    não-None. spread > 25 marca `inconclusive` (juízes discordando demais
    pra confiar num único número)."""
    values = [v for v in scores.values() if v is not None]
    median = statistics.median(values) if values else None
    spread = (max(values) - min(values)) if values else None
    return {
        "scores": scores,
        "median": median,
        "spread": spread,
        "inconclusive": bool(spread is not None and spread > SPREAD_INCONCLUSIVE_THRESHOLD),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_summary(summary: dict) -> Path:
    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    out = VERDICTS_DIR / f"summary_{harness_version()}.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return out


def run_all_judges(dry_run: bool) -> dict:
    scores: dict[str, int | None] = {}
    for judge_id in all_judge_ids():
        verdict = run_one(judge_id, dry_run)
        scores[judge_id] = verdict["judge_score"]

    summary = build_summary(scores)
    out = write_summary(summary)

    print("\n--- resumo --all-judges ---")
    for judge_id, score in summary["scores"].items():
        print(f"{judge_id}: {score}")
    print(f"median: {summary['median']}")
    print(f"spread: {summary['spread']}")
    if summary["inconclusive"]:
        print("inconclusive: true (spread > 25)")
    print(f"summary gravado em {out}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="orquestra a avaliação de um juiz (FASE 1)")
    ap.add_argument("--dry-run", action="store_true", help="pula run_task.py/persona real; monta verdict sintético")
    ap.add_argument(
        "--judge",
        default=DEFAULT_JUDGE_ID,
        choices=all_judge_ids(),
        help="qual juiz rodar (default: j_b2b, compatibilidade)",
    )
    ap.add_argument("--all-judges", action="store_true", help="roda os 3 juízes em sequência e agrega um summary")
    a = ap.parse_args()

    if a.all_judges:
        run_all_judges(a.dry_run)
        return 0

    run_one(a.judge, a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
