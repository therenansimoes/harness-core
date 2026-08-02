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
    5. verdict.json gravado em judges/verdicts/<judge_id>/<harness_version>.json
       (compat, sempre o mais recente) + um registro histórico timestamped em
       judges/verdicts/<judge_id>/history/<harness_version>_<ts>.json.

--dry-run: pula (2)-(4), usa números sintéticos + persona em modo mock
(PERSONA_MOCK=1), só para validar o formato do verdict (§7 do SPEC-J1).
Este harness ainda não faz nenhuma chamada paga — o caminho real acima
existe mas só foi exercitado com --dry-run / PERSONA_MOCK=1 até aqui.

--repeats N repete só o passo determinístico (1-3) N vezes por juiz, em
paralelo, agrega o score determinístico por MEDIANA e roda a persona uma vez
só, sobre a run mediana. Se as N runs discordarem entre si (spread_intra >
25), o juiz vira `unstable` e SE ABSTÉM: judge_score None, fora da mediana
entre juízes. N=1 (default) é o comportamento de sempre.

--all-judges roda os 3 em paralelo (real ou --dry-run) e agrega score por
juiz + mediana + spread num summary em
judges/verdicts/summary_<harness_version>.json. O paralelismo é por thread
(cada juiz é subprocess-bound: run_task.py, accept.py, `claude -p`), com a
saída de cada juiz bufferizada e impressa na ordem do registry.
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
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import persona  # noqa: E402
import process_metrics  # noqa: E402

DEFAULT_JUDGE_ID = "j_b2b"
REGISTRY = REPO_ROOT / "judges" / "registry.tsv"
VERDICTS_DIR = REPO_ROOT / "judges" / "verdicts"
RESULTS = REPO_ROOT / "results.tsv"
SPREAD_INCONCLUSIVE_THRESHOLD = 25

# Repetição intra-juiz (--repeats N). SPEC-J1 §8 já previa N>=3 com mediana;
# até aqui a mediana existia só ENTRE juízes, sobre UMA amostra por juiz —
# com o j_b2b bimodal (47/81 observados) isso é ruído institucionalizado.
# O número que justifica o custo Nx não é a mediana (mede 41%->30% de flip),
# é a ABSTENÇÃO: spread entre repetições > 25 => o juiz não repete, então ele
# não vota (flip 5.9%, 43% das avaliações seguem conclusivas — medição do
# candidato gen4/v2, arena/gen4/FEEDBACK-GEN4.md §2).
SPREAD_INTRA_UNSTABLE_THRESHOLD = 25

# --repeats N: quantas repetições do MESMO juiz rodam ao mesmo tempo. Cada
# repetição é uma run paga inteira (run_task.py + verify.py), por isso o teto
# baixo — e com --all-judges o total em voo é JUDGE_MAX_WORKERS x este.
REPEAT_MAX_WORKERS = 3

# --all-judges: quantos juízes rodam ao mesmo tempo. 3 = os 3 juízes da J1
# hoje; teto baixo de propósito, cada juiz sobe um agente/subprocess pesado.
JUDGE_MAX_WORKERS = 3

# Serializa os dois únicos pontos onde juízes paralelos compartilham
# recurso mutável: o append no results.tsv do repo e o stdout do processo.
_RESULTS_LOCK = threading.Lock()
_STDOUT_LOCK = threading.Lock()

# --------------------------------------------------------- trilha build (J2)
# SPEC-J2 §Design 2: mesma orquestração da trilha A (J1), trocando
# task_<judge_id>/verify.py por benchmarks/judge/<judge_id>/{brief.md,seed,
# accept.py} + judges/registry_build.tsv. Nenhum judge_id hardcoded além do
# default de compatibilidade.
DEFAULT_BUILD_JUDGE_ID = "build_j_b2b"
REGISTRY_BUILD = REPO_ROOT / "judges" / "registry_build.tsv"

# pesos da régua J2, trilha build (RUBRIC-J2.md). D1 não existe na trilha B
# (sem upstream a corrigir — B1 é quem mede "projeto de verdade"). D3 não
# tem dado na trilha B (não há "full suite" colateral, só o accept.py
# selado) — fica sempre em discarded, ver build_verdict_build.
WEIGHTS_BUILD = {"D2": 15, "B1": 15, "P1": 15, "X1": 10, "X2": 10, "X3": 5, "P3": 10, "P4": 5}


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


def synthetic_deterministic(run_idx: int = 0) -> dict:
    """Números sintéticos pro --dry-run — não reflete run nenhuma real.

    `run_idx` injeta variação DETERMINÍSTICA por repetição (--repeats N), pra
    o dry-run exercitar mediana/spread_intra/abstenção sem chamar API: a
    penalidade em D1 cresce 5 por índice, saturando no peso de D1. Assim
    N<=4 fica estável (spread <= 25) e N>=5 estoura o limiar e cai na
    abstenção. `run_idx=0` devolve exatamente os números de antes."""
    penalty = min(run_idx * 5, WEIGHTS["D1"])
    return {
        "D1": WEIGHTS["D1"] - penalty,
        "D2": WEIGHTS["D2"],
        "D3": WEIGHTS["D3"],
        "D4": WEIGHTS["D4"],
        "veto": False,
        "evidence": {
            "target_test": "passed (sintético --dry-run)",
            "full_suite": "414 passed, 0 failed (sintético --dry-run)",
            "cost_usd": round(0.42 + run_idx * 0.01, 4),
            "turns": 4,
        },
    }


def deterministic_score(deterministic: dict) -> int:
    """Score 0-100 só da parte determinística (D1-D4), que é o que se repete
    em --repeats N — a persona roda uma vez só, sobre a run mediana, então
    ela não pode entrar na amostra que mede a variância do juiz."""
    if deterministic.get("veto"):
        return 0
    numer = sum(deterministic[k] for k in ("D1", "D2", "D3", "D4"))
    denom = sum(WEIGHTS[k] for k in ("D1", "D2", "D3", "D4"))
    return round(numer / denom * 100) if denom > 0 else 0


def aggregate_repeats(scores_runs: list[int]) -> dict:
    """Agrega as N repetições do MESMO juiz: mediana + spread_intra (max-min)
    + `unstable`. Função pura (sem I/O, sem relógio) de propósito — é o que os
    testes exercitam sem pagar run nenhuma."""
    median = statistics.median(scores_runs) if scores_runs else None
    spread_intra = (max(scores_runs) - min(scores_runs)) if scores_runs else None
    return {
        "repeats": len(scores_runs),
        "scores_runs": list(scores_runs),
        "median": median,
        "spread_intra": spread_intra,
        "unstable": bool(spread_intra is not None and spread_intra > SPREAD_INTRA_UNSTABLE_THRESHOLD),
    }


def median_run_index(scores_runs: list[int], costs: list[float]) -> int:
    """Índice da run MEDIANA — a que a persona vai julgar. Com N par a
    mediana estatística cai entre duas runs e não existe run nenhuma com
    aquele score: pega a mais próxima da mediana, e empate desempata pela
    mais barata (menor cost_usd; empate nisso também, a primeira)."""
    median = statistics.median(scores_runs)
    return min(
        range(len(scores_runs)),
        key=lambda i: (abs(scores_runs[i] - median), costs[i], i),
    )


def with_repeats(verdict: dict, agg: dict) -> dict:
    """Campos aditivos de --repeats no verdict (sempre presentes na trilha
    result; com N=1 são repeats=1/spread_intra=0/unstable=False, o que
    descreve o comportamento de sempre em vez de escondê-lo)."""
    verdict["repeats"] = agg["repeats"]
    verdict["scores_runs"] = agg["scores_runs"]
    verdict["spread_intra"] = agg["spread_intra"]
    verdict["unstable"] = agg["unstable"]
    return verdict


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


def _score_persona_criteria(
    ficha: dict, keys: tuple[str, ...], material: dict[str, str]
) -> tuple[dict, list[str], bool, str]:
    """Regra de citação compartilhada entre a trilha A (P1/P2) e a trilha
    build (P1/P3/P4): sem citação -> descartado (sai do cálculo); citação
    cujo `quote` não aparece no `material[key]` correspondente -> **veto de
    persona** (zera todos os `keys`, não só o critério ofensor). Quem
    decide o que fazer com o veto é build_verdict/build_verdict_build (via
    o retorno `persona_vetoed`)."""
    scored, discarded = {}, []
    for key in keys:
        entry = ficha.get(key) or {}
        citation = (entry.get("citation") or "").strip()
        quote = (entry.get("quote") or "").strip()
        if not citation:
            discarded.append(key)
            continue
        if not quote or _normalize_for_match(quote) not in material[key]:
            reason = f"persona vetada: citação inválida em {key}: {citation!r} não sustentada pelo material"
            return {}, list(keys), True, reason
        scored[key] = {
            "score": int(entry.get("score", 0)),
            "citation": citation,
            "quote": quote,
        }
    return scored, discarded, False, ""


def validate_and_score_persona(ficha: dict, diff: str, trace: str) -> tuple[dict, list[str], bool, str]:
    """Aplica a regra de citação do RUBRIC-J1: sem citação -> descartado
    (sai do cálculo); citação cujo `quote` não aparece no material
    correspondente (diff pra P1, trace pra P2) -> **veto de persona**:
    P1 e P2 saem do cálculo (viram discarded), mas D1-D4 NÃO são afetados
    — isso é diferente do veto de candidato (D2, trapaça), que zera a
    ficha inteira. Quem decide o que fazer com o veto de persona é
    build_verdict (via o retorno `persona_vetoed`)."""
    material = {"P1": _normalize_for_match(diff), "P2": _normalize_for_match(trace)}
    return _score_persona_criteria(ficha, ("P1", "P2"), material)


def validate_and_score_persona_build(ficha: dict, trace: str, artifact: str) -> tuple[dict, list[str], bool, str]:
    """Mesma regra de citação/veto acima (RUBRIC-J2 §Regra de citação e
    veto), trilha build: P1 e P4 citam o artefato entregue (`pricing.py`
    do workspace — não há diff upstream na trilha B, o arquivo inteiro é
    novo), P3 cita `trace.jsonl:N` do par erro→correção."""
    material = {
        "P1": _normalize_for_match(artifact),
        "P3": _normalize_for_match(trace),
        "P4": _normalize_for_match(artifact),
    }
    return _score_persona_criteria(ficha, ("P1", "P3", "P4"), material)


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


def build_unstable_verdict(judge_id: str, reg: dict, agg: dict, deterministic: dict, cost_usd: float) -> dict:
    """Abstenção por variância do próprio juiz: as N repetições discordaram
    entre si mais que SPREAD_INTRA_UNSTABLE_THRESHOLD, então o juiz não tem
    um número pra dar. `judge_score` fica None — mesma convenção do
    infra_error: None sai da mediana ENTRE juízes em vez de contaminá-la
    (0 significaria "trabalho ruim"). A persona NÃO é chamada: ela custa e
    só serviria pra decorar um voto que não vai ser contado.

    O `deterministic` da run mediana fica no verdict como evidência do que
    foi medido — não pontua (judge_score é None), é rastro."""
    verdict = {
        "judge_id": judge_id,
        "harness_version": harness_version(),
        "rubric_version": reg["rubric_version"],
        "base_sha": reg["base_sha"],
        "sealed_sha256": reg["sealed_sha256"],
        "deterministic": deterministic,
        "persona": {},
        "discarded": [],
        "persona_vetoed": False,
        "veto_reason": "",
        "judge_score": None,
        "unstable_reason": (
            f"spread_intra={agg['spread_intra']} > {SPREAD_INTRA_UNSTABLE_THRESHOLD} "
            f"em {agg['repeats']} repetições ({agg['scores_runs']}) — juiz se abstém"
        ),
        "cost_usd": cost_usd,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return with_repeats(verdict, agg)


def build_dir_for(judge_id: str) -> Path:
    """Trilha build: `benchmarks/judge/<judge_id>/` (SEM o prefixo `task_`
    de propósito — é assim que `run_task.py` deixa de descobrir essas
    pastas como task normal, ver RUBRIC-J2 §Trilha B)."""
    return REPO_ROOT / "benchmarks" / "judge" / judge_id


def all_build_judge_ids() -> list[str]:
    """judge_id da trilha build, na ordem do registry_build.tsv. Espelha
    all_judge_ids(), mas registry_build.tsv só tem entradas J2 — sem
    filtro de rubric_version."""
    lines = REGISTRY_BUILD.read_text().splitlines()
    header = lines[0].split("\t")
    idx = header.index("judge_id")
    return [cols[idx] for line in lines[1:] if line.strip() for cols in [line.split("\t")]]


def read_registry_build_row(judge_id: str) -> dict:
    lines = REGISTRY_BUILD.read_text().splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        cols = line.split("\t")
        row = dict(zip(header, cols))
        if row.get("judge_id") == judge_id:
            return row
    raise SystemExit(f"{judge_id} não encontrado em {REGISTRY_BUILD}")


def build_tampered(ws: Path) -> bool:
    """D2 na trilha build (RUBRIC-J2 §D2): `accept.py` real é rodado por
    fora do workspace e sempre resseala `test_pricing.py` a partir do
    arquivo selado (mesmo mecanismo da J1) — então um `accept.py`/
    `test_pricing.py` forjado pelo agente não muda o resultado do accept,
    mas a PRESENÇA desses arquivos no workspace já é a tentativa de forjar
    o verificador ou o critério de aceite, que o brief proíbe."""
    return (ws / "accept.py").exists() or (ws / "test_pricing.py").exists()


def run_accept_build(judge_id: str, ws: Path) -> tuple[bool, str]:
    """Roda o `accept.py` (não-selado, vive em benchmarks/judge/<judge_id>/
    — ele mesmo verifica o sha256 do teste selado contra registry_build.tsv
    e injeta a cópia real no workspace só na hora de verificar) contra
    `ws`. exit 0 = B1 verde."""
    accept_path = build_dir_for(judge_id) / "accept.py"
    proc = subprocess.run(
        [sys.executable, str(accept_path), str(ws)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return proc.returncode == 0, proc.stdout + proc.stderr


def compute_deterministic_build(b1_ok: bool, tampered: bool, accept_out: str) -> dict:
    """D1-D4 da J1 não se aplicam por inteiro na trilha build: D1 não
    existe (sem upstream — B1 é quem mede "projeto de verdade"); D3 fica
    sempre em discarded no verdict (build_verdict_build) — não há "full
    suite" colateral na trilha build, só o accept.py selado."""
    veto = bool(tampered)
    d2 = 0 if veto else WEIGHTS_BUILD["D2"]
    b1 = WEIGHTS_BUILD["B1"] if (b1_ok and not veto) else 0
    return {
        "D2": d2,
        "B1": b1,
        "veto": veto,
        "evidence": {
            "accept": "passed" if b1_ok else "failed",
            "accept_output": accept_out[-2000:],
        },
    }


def render_trace_path(trace_path: Path) -> str:
    """Mesmo formato de load_trace (`"{i}: {line}"`), mas a partir de um
    Path direto — a trilha build não passa pelas notes do results.tsv."""
    if not trace_path.exists():
        return ""
    lines = trace_path.read_text().splitlines()
    return "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=1))


def build_verdict_build(
    judge_id: str,
    reg: dict,
    deterministic: dict,
    process: dict,
    persona_scored: dict,
    discarded: list[str],
    persona_vetoed: bool,
    veto_reason: str,
    cost_usd: float,
) -> dict:
    """Aditivo sobre build_verdict (RUBRIC-J2 §Agregação): track/build_id/
    process, mesmo mecanismo de discarded/veto/denominador dinâmico."""
    candidate_veto = bool(deterministic.get("veto", False))
    if candidate_veto and not veto_reason:
        veto_reason = "D2: accept.py/test_pricing.py forjado no workspace"

    all_discarded = ["D3"] + list(discarded)  # D3: sem "full suite" na trilha build
    process_scores = {}
    for key in ("X1", "X2", "X3"):
        val = process.get(key)
        if val == process_metrics.DISCARDED:
            all_discarded.append(key)
        else:
            process_scores[key] = val

    if candidate_veto:
        judge_score = 0
    else:
        numer = float(deterministic["D2"] + deterministic["B1"])
        denom = WEIGHTS_BUILD["D2"] + WEIGHTS_BUILD["B1"]
        for key, val in process_scores.items():
            numer += val
            denom += WEIGHTS_BUILD[key]
        for key, entry in persona_scored.items():
            numer += entry["score"]
            denom += WEIGHTS_BUILD[key]
        judge_score = round(numer / denom * 100) if denom > 0 else 0

    return {
        "judge_id": judge_id,
        "harness_version": harness_version(),
        "rubric_version": reg["rubric_version"],
        "track": "build",
        "build_id": judge_id,
        "base_sha": reg.get("base_sha", "n/a"),
        "sealed_sha256": reg["sealed_sha256"],
        "deterministic": deterministic,
        "process": process,
        "persona": persona_scored,
        "discarded": all_discarded,
        "persona_vetoed": persona_vetoed,
        "veto_reason": veto_reason,
        "judge_score": judge_score,
        "cost_usd": cost_usd,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_verdict(verdict: dict) -> Path:
    """Grava o verdict em dois lugares: um registro histórico timestamped
    (`history/<versão>_<ts compacto>.json`, um por run — nada é
    sobrescrito) e a cópia `<versão>.json` de compatibilidade (path que
    summary/testes/graph.ingest_verdicts já esperam, sempre a mais
    recente). `history/` fica um nível a mais fundo que `judges/verdicts/
    <judge_id>/`, então o glob `*/*.json` de `graph.ingest_verdicts` não
    desce até lá — só a cópia de compat é ingerida, sem duplicar linha."""
    out_dir = VERDICTS_DIR / verdict["judge_id"]
    history_dir = out_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    ts_compact = datetime.fromisoformat(verdict["ts"]).strftime("%Y%m%dT%H%M%S")
    payload = json.dumps(verdict, indent=2, ensure_ascii=False) + "\n"

    hist_path = history_dir / f"{verdict['harness_version']}_{ts_compact}.json"
    hist_path.write_text(payload)

    out = out_dir / f"{verdict['harness_version']}.json"
    out.write_text(payload)
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


def run_real(judge_id: str = DEFAULT_JUDGE_ID, repeats: int = 1) -> dict:
    """Roda run_task.py de verdade (agente real via `claude -p`, custo
    real) e monta a ficha a partir do resultado. Não exercitado neste PR
    (proibido chamar API paga) — implementado, não testado end-to-end.

    Com `repeats` > 1 a parte DETERMINÍSTICA roda N vezes (em paralelo, cada
    uma com workspace e results.tsv próprios); a persona roda UMA vez, sobre
    a run mediana. Se as N runs discordarem demais entre si
    (spread_intra > 25), o juiz se abstém e a persona nem é chamada."""
    reg = read_registry_row(judge_id)
    runs = _run_deterministic_repeats(judge_id, repeats)
    try:
        ok = [r for r in runs if not r.get("infra_error")]
        if not ok:
            # nenhuma repetição chegou a trabalhar: infra quebrada, não
            # variância — mesmo curto-circuito de sempre.
            return build_infra_error_verdict(judge_id, reg, runs[0].get("row"))

        scores_runs = [deterministic_score(r["deterministic"]) for r in ok]
        agg = aggregate_repeats(scores_runs)
        costs = [r["cost_usd"] for r in ok]
        run = ok[median_run_index(scores_runs, costs)]

        if agg["unstable"]:
            return build_unstable_verdict(judge_id, reg, agg, run["deterministic"], run["cost_usd"])

        ficha = persona.call_persona(run["deterministic"], run["diff"], run["trace"], run["verify_out"])
        persona_scored, discarded, persona_vetoed, veto_reason = validate_and_score_persona(
            ficha, run["diff"], run["trace"]
        )
        verdict = build_verdict(
            judge_id, reg, run["deterministic"], persona_scored, discarded, persona_vetoed,
            veto_reason, run["cost_usd"],
        )
        return with_repeats(verdict, agg)
    finally:
        for r in runs:
            if r.get("ws"):
                shutil.rmtree(r["ws"], ignore_errors=True)


def _run_deterministic_repeats(judge_id: str, repeats: int) -> list[dict]:
    """As N repetições do determinístico. N=1 roda inline (nenhuma thread a
    mais que antes); N>1 usa pool próprio — e como cada repetição já é
    subprocess-bound e tem results.tsv/workspace exclusivos, elas não
    compartilham nada mutável além do que merge_judge_results já serializa."""
    def one(_i: int) -> dict:
        tmp_dir = Path(tempfile.mkdtemp(prefix="harness_judge_results_"))
        try:
            return _run_deterministic_once(judge_id, tmp_dir / "results.tsv")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    n = max(1, repeats)
    if n == 1:
        return [one(0)]
    with ThreadPoolExecutor(max_workers=min(REPEAT_MAX_WORKERS, n)) as pool:
        return list(pool.map(one, range(n)))


def _run_deterministic_once(judge_id: str, results_path: Path) -> dict:
    """UMA run determinística: agente + verify.py. Devolve o material bruto
    (deterministic/diff/trace/verify_out/cost + o workspace, que fica de pé
    — quem chamou decide qual run a persona julga e faz a limpeza), ou
    `{"infra_error": True, ...}` quando o agente consumiu 0 tokens.

    O run_task.py deste juiz grava num results.tsv EXCLUSIVO (via
    HARNESS_RESULTS, mesmo padrão de experiment.run_task_launch): com
    --all-judges paralelo (e agora também com --repeats N), dois run_task.py
    coexistem e appends intercalados no results.tsv do repo poderiam quebrar
    a leitura da "última linha do task_id". As linhas voltam pro results.tsv
    do repo em merge_judge_results(), sob lock — o histórico continua lá (é
    dele que median_baseline_cost_turns vive)."""
    task_dir = task_dir_for(judge_id)
    task_id = f"task_{judge_id}"

    if not task_dir.joinpath("fixtures", "pyproject.toml").exists():
        subprocess.run(["bash", str(task_dir / "setup.sh")], check=True)

    env = dict(os.environ)
    env["HARNESS_RESULTS"] = str(results_path)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run_task.py"), str(task_dir), "--suite", "judge", "--keep"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=700,
    )
    with _STDOUT_LOCK:
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

    row = last_results_row(task_id, results_path)
    merge_judge_results(results_path)
    tokens = int(row["tokens"]) if row and row.get("tokens", "").strip() else 0
    if tokens == 0:
        # infra quebrou antes do agente trabalhar (0 tokens = nenhuma
        # chamada ao modelo aconteceu) — curto-circuita: sem persona, sem
        # D1-D4, judge_score fica None em vez de um 0 que pareceria "o
        # agente tentou e foi mal".
        shutil.rmtree(ws, ignore_errors=True)
        return {"infra_error": True, "row": row}

    cost_usd = float(row["cost_usd"]) if row else 0.0
    turns = int(row["turns"]) if row else 0
    # run_task.py grava "tamper:test_file_modified" nas notes quando o
    # agente editou test_*.py em vez de resolver — é o único sinal de
    # tamper que ele expõe pra fora hoje.
    tampered = bool(row and row.get("notes", "").startswith("tamper:"))

    trace = load_trace(row.get("notes", "") if row else "")

    deterministic = compute_deterministic(verify_result, tampered, cost_usd, turns, task_id)
    return {
        "deterministic": deterministic,
        "diff": diff,
        "trace": trace,
        "verify_out": verify_out,
        "cost_usd": cost_usd,
        "ws": ws,
    }


def merge_judge_results(results_path: Path) -> None:
    """Anexa as linhas de dados de um results.tsv exclusivo de juiz ao
    results.tsv do repo. Único ponto de escrita compartilhada entre juízes
    paralelos, por isso o lock — o resto do fluxo de cada juiz é
    workspace/arquivo próprio."""
    if not results_path.exists():
        return
    lines = [line for line in results_path.read_text().splitlines() if line.strip()]
    if len(lines) < 2:
        return
    with _RESULTS_LOCK:
        if not RESULTS.exists():
            RESULTS.write_text(lines[0] + "\n")
        with RESULTS.open("a") as fh:
            for line in lines[1:]:
                fh.write(line + "\n")


def last_results_row(task_id: str, results_path: Path | None = None) -> dict | None:
    """`results_path` default = results.tsv do repo (compat); run_real
    passa o arquivo exclusivo daquele juiz."""
    results = results_path or RESULTS
    if not results.exists():
        return None
    header = None
    last = None
    with results.open() as fh:
        for i, line in enumerate(fh):
            cols = line.rstrip("\n").split("\t")
            if i == 0:
                header = cols
                continue
            row = dict(zip(header, cols))
            if row.get("task_id") == task_id:
                last = row
    return last


def run_dry(judge_id: str = DEFAULT_JUDGE_ID, repeats: int = 1) -> dict:
    """`repeats` N simula N runs determinísticas com scores DISTINTOS
    (synthetic_deterministic(i)) e passa pelo mesmo agregador do caminho
    real: mediana, spread_intra, abstenção. Serve pra provar formato e
    wiring — não mede variância nenhuma, que é sintética aqui."""
    reg = read_registry_row(judge_id)
    os.environ.setdefault("PERSONA_MOCK", "1")

    runs = [synthetic_deterministic(i) for i in range(max(1, repeats))]
    scores_runs = [deterministic_score(d) for d in runs]
    agg = aggregate_repeats(scores_runs)
    costs = [d["evidence"]["cost_usd"] for d in runs]
    deterministic = runs[median_run_index(scores_runs, costs)]
    cost_usd = deterministic["evidence"]["cost_usd"]

    if agg["unstable"]:
        return build_unstable_verdict(judge_id, reg, agg, deterministic, cost_usd)

    diff = "schwifty/checksum/germany.py:1\n-        return checksum\n+        return super().reconcile(checksum)\n"
    trace = "trace.jsonl:1 DONE: corrigido Algorithm11.reconcile para delegar ao método base\n"
    ficha = persona.call_persona(deterministic, diff, trace, "78 passed\n414 passed")
    persona_scored, discarded, persona_vetoed, veto_reason = validate_and_score_persona(ficha, diff, trace)
    verdict = build_verdict(
        judge_id, reg, deterministic, persona_scored, discarded, persona_vetoed, veto_reason, cost_usd,
    )
    return with_repeats(verdict, agg)


# --------------------------------------------------------- trilha build (J2)

# Implementação de referência de pricing.py usada SÓ no --dry-run (sem
# agente real, sem custo de API) pra exercitar accept.py + process_metrics
# de ponta a ponta contra um workspace que de fato satisfaz o brief —
# B1 vermelho contra o seed intocado é responsabilidade do accept.py em
# si (rodado direto contra benchmarks/judge/build_j_b2b/seed/), não deste
# harness.
_SYNTHETIC_PRICING_SOLUTION = '''"""pricing.py — cotação de pedidos B2B (implementação sintética do --dry-run
de run_judge.py --track build; não é o agente real, só exercita o fluxo)."""
from __future__ import annotations

VOLUME_DISCOUNT_TIERS = [(100, 10), (50, 5)]
FREE_SHIPPING_THRESHOLD_CENTS = 500_000
FLAT_SHIPPING_CENTS = 2_500


def _validate_items(items: list[dict]) -> str | None:
    if not items:
        return "carrinho vazio"
    for item in items:
        if item.get("qty", 0) <= 0 or item.get("unit_price_cents", 0) <= 0:
            return "qty e unit_price_cents devem ser positivos"
    return None


def _subtotal_cents(items: list[dict]) -> int:
    return sum(item["qty"] * item["unit_price_cents"] for item in items)


def _discount_percent(total_qty: int) -> int:
    for threshold, pct in VOLUME_DISCOUNT_TIERS:
        if total_qty >= threshold:
            return pct
    return 0


def _shipping_cents(discounted_subtotal_cents: int) -> int:
    return 0 if discounted_subtotal_cents >= FREE_SHIPPING_THRESHOLD_CENTS else FLAT_SHIPPING_CENTS


def calculate_total(items: list[dict]) -> dict:
    subtotal = _subtotal_cents(items)
    total_qty = sum(item["qty"] for item in items)
    discount_percent = _discount_percent(total_qty)
    discounted = subtotal - (subtotal * discount_percent) // 100
    shipping = _shipping_cents(discounted)
    return {
        "subtotal_cents": subtotal,
        "discount_percent": discount_percent,
        "shipping_cents": shipping,
        "total_cents": discounted + shipping,
    }


def handle_quote_request(payload: dict) -> dict:
    items = payload.get("items") if isinstance(payload, dict) else None
    error = _validate_items(items or [])
    if error:
        return {"status": 400, "body": {"error": error}}
    return {"status": 200, "body": calculate_total(items)}
'''


def _synthetic_build_trace_events() -> list[dict]:
    """3 erros / 2 recuperações (mesmo cenário de
    tests/test_process_metrics.py::_synthetic_3_erros_2_recuperacoes) — só
    pra X1/X2/X3 saírem de números não-triviais no --dry-run, sem custo de
    API."""
    err_cmd = {"command": "pytest tests/foo.py"}
    fix_cmd = {"command": "pytest tests/foo.py -v"}
    return [
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": err_cmd}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "ImportError: no module named foo"}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": err_cmd}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "ImportError: no module named foo"}]}},
        {"type": "assistant", "message": {"id": "m3", "content": [{"type": "tool_use", "id": "t3", "name": "Bash", "input": err_cmd}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "ImportError: no module named foo"}]}},
        {"type": "assistant", "message": {"id": "m4", "content": [{"type": "text", "text": "deixa eu conferir o import"}]}},
        {"type": "assistant", "message": {"id": "m5", "content": [{"type": "tool_use", "id": "t5", "name": "Bash", "input": fix_cmd}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t5", "is_error": False, "content": "1 passed"}]}},
        {"type": "result", "is_error": False, "num_turns": 5},
    ]


def run_dry_build(judge_id: str = DEFAULT_BUILD_JUDGE_ID) -> dict:
    """--dry-run da trilha build (SPEC-J2 design 2): pula o agente real,
    escreve uma implementação sintética de pricing.py + um trace.jsonl
    sintético no workspace, e roda o resto do fluxo real (accept.py,
    process_metrics, persona em modo mock) — só pra validar o formato do
    verdict J2, igual run_dry faz pra J1."""
    reg = read_registry_build_row(judge_id)
    bdir = build_dir_for(judge_id)
    os.environ.setdefault("PERSONA_MOCK", "1")

    with tempfile.TemporaryDirectory(prefix="harness_build_dry_") as tmp:
        tmp_path = Path(tmp)
        ws = tmp_path / "ws"
        shutil.copytree(bdir / "seed", ws)
        (ws / "pricing.py").write_text(_SYNTHETIC_PRICING_SOLUTION)

        b1_ok, accept_out = run_accept_build(judge_id, ws)
        tampered = build_tampered(ws)
        artifact = (ws / "pricing.py").read_text()

        trace_path = tmp_path / "trace.jsonl"
        trace_path.write_text("\n".join(json.dumps(e) for e in _synthetic_build_trace_events()) + "\n")
        metrics = process_metrics.parse_trace(trace_path)
        trace_rendered = render_trace_path(trace_path)

    cost_usd = 0.35
    baseline = median_baseline_cost_turns(judge_id)
    process = {
        "X1": process_metrics.X1(metrics),
        "X2": process_metrics.X2(metrics),
        "X3": process_metrics.X3(metrics["n_turns"], cost_usd, baseline),
        "metrics": metrics,
    }

    deterministic = compute_deterministic_build(b1_ok, tampered, accept_out)
    ficha = persona.call_persona_build(deterministic, trace_rendered, artifact, process)
    persona_scored, discarded, persona_vetoed, veto_reason = validate_and_score_persona_build(
        ficha, trace_rendered, artifact
    )

    return build_verdict_build(
        judge_id, reg, deterministic, process, persona_scored, discarded, persona_vetoed, veto_reason, cost_usd
    )


def run_real_build(judge_id: str = DEFAULT_BUILD_JUDGE_ID) -> dict:
    """Roda o agente real (via agent.run_agent, custo real) contra a
    trilha build: copia seed/ pro workspace, usa brief.md como prompt,
    injeta/roda accept.py só na verificação. Não exercitado neste PR
    (proibido chamar API paga) — implementado, não testado end-to-end,
    mesmo status de run_real() na trilha A."""
    import agent  # noqa: E402 (import local — só o caminho real precisa do módulo do agente)

    reg = read_registry_build_row(judge_id)
    bdir = build_dir_for(judge_id)
    brief = (bdir / "brief.md").read_text()

    run_id = f"harness_build_{judge_id}_{os.urandom(4).hex()}"
    ws = REPO_ROOT / ".harness_build_ws" / run_id
    ws.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bdir / "seed", ws)

    # Não setar HARNESS_RUN_ID aqui (era o que este trecho fazia): os.environ é
    # global do processo e com --all-judges paralelo um juiz sobrescreveria o
    # run_id do outro. Como ws.name == run_id por construção acima, agent.py
    # chega no mesmo run_id pelo fallback workspace.name (agent.py:184) — o pop
    # só garante que um HARNESS_RUN_ID herdado do pai não vença esse fallback,
    # e é idempotente, então não corre risco entre threads.
    os.environ.pop("HARNESS_RUN_ID", None)
    result = agent.run_agent(brief, ws)

    if result.tokens == 0:
        # mesmo curto-circuito de run_real(): 0 tokens = infra quebrada
        # antes do agente trabalhar, não "trabalho ruim".
        shutil.rmtree(ws, ignore_errors=True)
        return build_infra_error_verdict(judge_id, reg, {"tokens": "0", "notes": result.notes})

    b1_ok, accept_out = run_accept_build(judge_id, ws)
    tampered = build_tampered(ws)
    artifact = (ws / "pricing.py").read_text() if (ws / "pricing.py").exists() else ""

    metrics = {
        "n_turns": result.turns, "n_tool_calls": 0, "n_tool_errors": 0, "n_recovered": 0,
        "n_thrash": 0, "n_help_requests": 0, "stop_reason": "success" if result.ok else "error",
    }
    trace_rendered = ""
    if result.trace_path:
        trace_full = REPO_ROOT / result.trace_path
        if trace_full.exists():
            metrics = process_metrics.parse_trace(trace_full)
            trace_rendered = render_trace_path(trace_full)

    cost_usd = result.cost_usd
    baseline = median_baseline_cost_turns(judge_id)
    process = {
        "X1": process_metrics.X1(metrics),
        "X2": process_metrics.X2(metrics),
        "X3": process_metrics.X3(metrics["n_turns"], cost_usd, baseline),
        "metrics": metrics,
    }

    deterministic = compute_deterministic_build(b1_ok, tampered, accept_out)
    ficha = persona.call_persona_build(deterministic, trace_rendered, artifact, process)
    persona_scored, discarded, persona_vetoed, veto_reason = validate_and_score_persona_build(
        ficha, trace_rendered, artifact
    )

    shutil.rmtree(ws, ignore_errors=True)

    return build_verdict_build(
        judge_id, reg, deterministic, process, persona_scored, discarded, persona_vetoed, veto_reason, cost_usd
    )


def run_all_judges_build(dry_run: bool) -> dict:
    """Espelha run_all_judges (mesmo paralelismo) pra trilha build — hoje só build_j_b2b em
    registry_build.tsv, mas generalizado do mesmo jeito (nada hardcoded
    além do default de compatibilidade)."""
    verdicts = run_judges_parallel(all_build_judge_ids(), dry_run, track="build")
    summary = build_summary({judge_id: v["judge_score"] for judge_id, v in verdicts.items()})
    out = write_summary(summary, prefix="summary_build")

    print("\n--- resumo --all-judges (trilha build) ---")
    for judge_id, score in summary["scores"].items():
        print(f"{judge_id}: {score}")
    print(f"median: {summary['median']}")
    print(f"spread: {summary['spread']}")
    if summary["inconclusive"]:
        print("inconclusive: true (spread > 25)")
    print(f"summary gravado em {out}")
    return summary


# ------------------------------------------------------------------ --all-judges


def run_one_quiet(judge_id: str, dry_run: bool, track: str = "result", repeats: int = 1) -> tuple[dict, str]:
    """Roda um juiz e devolve (verdict, saída) em vez de imprimir — é o que
    o modo paralelo usa pra não intercalar linhas de dois juízes no stdout."""
    if track == "build":
        verdict = run_dry_build(judge_id) if dry_run else run_real_build(judge_id)
    else:
        verdict = run_dry(judge_id, repeats) if dry_run else run_real(judge_id, repeats)
    out = write_verdict(verdict)
    lines = [
        json.dumps(verdict, indent=2, ensure_ascii=False),
        f"\nverdict gravado em {out}",
        f"judge_score = {verdict['judge_score']}",
    ]
    if verdict.get("unstable"):
        lines.append(f"UNSTABLE (abstenção): {verdict['unstable_reason']}")
    return verdict, "\n".join(lines)


def run_one(judge_id: str, dry_run: bool, track: str = "result", repeats: int = 1) -> dict:
    verdict, report = run_one_quiet(judge_id, dry_run, track, repeats)
    print(report)
    return verdict


def run_judges_parallel(judge_ids: list[str], dry_run: bool, track: str, repeats: int = 1) -> dict[str, dict]:
    """Roda os juízes da trilha em paralelo (thread basta: cada juiz é
    subprocess-bound — run_task.py/agent, accept.py, `claude -p`) e devolve
    os verdicts na ordem do registry, determinística, independente de quem
    terminou primeiro. A saída de cada juiz sai inteira, em ordem, no fim."""
    if not judge_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(JUDGE_MAX_WORKERS, len(judge_ids))) as pool:
        futures = [pool.submit(run_one_quiet, judge_id, dry_run, track, repeats) for judge_id in judge_ids]
        results = [f.result() for f in futures]

    verdicts: dict[str, dict] = {}
    for judge_id, (verdict, report) in zip(judge_ids, results):
        print(report)
        verdicts[judge_id] = verdict
    return verdicts


def intra_from_verdicts(verdicts: dict[str, dict]) -> dict[str, dict]:
    """Recorte de --repeats de cada verdict, pro summary — só o que o
    summary precisa pra registrar quem se absteve e por quê."""
    return {
        judge_id: {
            "repeats": v.get("repeats", 1),
            "scores_runs": v.get("scores_runs", []),
            "spread_intra": v.get("spread_intra"),
            "unstable": bool(v.get("unstable")),
        }
        for judge_id, v in verdicts.items()
    }


def build_summary(scores: dict[str, int | None], intra: dict[str, dict] | None = None) -> dict:
    """Agrega score por juiz: mediana + spread (max-min) sobre os scores
    não-None. spread > 25 marca `inconclusive` (juízes discordando demais
    pra confiar num único número).

    `intra` (o recorte de --repeats por juiz) separa as DUAS causas de
    dúvida que antes viviam fundidas num `inconclusive` só: `disagreement`
    (juízes divergem entre si) e `variance_intra` (o próprio juiz não
    repete — abstenção, score já vem None e portanto fora da mediana).
    Sem `intra` — o caso N=1 — o formato antigo sai igual, chave por chave."""
    values = [v for v in scores.values() if v is not None]
    median = statistics.median(values) if values else None
    spread = (max(values) - min(values)) if values else None
    disagreement = bool(spread is not None and spread > SPREAD_INCONCLUSIVE_THRESHOLD)
    summary = {
        "scores": scores,
        "median": median,
        "spread": spread,
        "inconclusive": disagreement,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if intra is None:
        return summary

    unstable = sorted(j for j, r in intra.items() if r.get("unstable"))
    reasons = (["disagreement"] if disagreement else []) + (["variance_intra"] if unstable else [])
    summary["intra"] = intra
    summary["repeats"] = max((r.get("repeats", 1) for r in intra.values()), default=1)
    summary["unstable_judges"] = unstable
    # abstenção é motivo de não decidir: quem se absteve saiu da mediana, e
    # decidir com o que sobrou seria trocar "não sei" por um número menos
    # amostrado. É o mecanismo que leva o flip a 5.9% (FEEDBACK-GEN4 §2).
    summary["inconclusive"] = bool(disagreement or unstable)
    summary["inconclusive_reason"] = reasons
    return summary


def write_summary(summary: dict, prefix: str = "summary") -> Path:
    """`prefix` default preserva o nome de arquivo de sempre (trilha A);
    a trilha build usa `summary_build` pra não pisar no summary da J1."""
    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    out = VERDICTS_DIR / f"{prefix}_{harness_version()}.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return out


def run_all_judges(dry_run: bool, repeats: int = 1) -> dict:
    verdicts = run_judges_parallel(all_judge_ids(), dry_run, track="result", repeats=repeats)
    scores = {judge_id: v["judge_score"] for judge_id, v in verdicts.items()}
    intra = intra_from_verdicts(verdicts) if repeats > 1 else None
    summary = build_summary(scores, intra)
    out = write_summary(summary)

    print("\n--- resumo --all-judges ---")
    for judge_id, score in summary["scores"].items():
        line = f"{judge_id}: {score}"
        if intra is not None:
            r = intra[judge_id]
            line += f"  (n={r['repeats']} runs={r['scores_runs']} spread_intra={r['spread_intra']}"
            line += " UNSTABLE/abstenção)" if r["unstable"] else ")"
        print(line)
    print(f"median: {summary['median']}")
    print(f"spread: {summary['spread']}")
    if summary["inconclusive"]:
        reason = ",".join(summary.get("inconclusive_reason") or ["disagreement"])
        print(f"inconclusive: true ({reason})")
    print(f"summary gravado em {out}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="orquestra a avaliação de um juiz (FASE 1 + trilha build/J2)")
    ap.add_argument("--dry-run", action="store_true", help="pula run_task.py/persona real; monta verdict sintético")
    ap.add_argument(
        "--track",
        default="result",
        choices=["result", "build"],
        help="trilha J1 'result' (default, compatibilidade) ou trilha build 'build' (SPEC-J2 design 2)",
    )
    ap.add_argument(
        "--judge",
        default=None,
        help="qual judge_id rodar (default: j_b2b em --track result, build_j_b2b em --track build)",
    )
    ap.add_argument(
        "--all-judges",
        action="store_true",
        help=f"roda os juízes da trilha em paralelo (até {JUDGE_MAX_WORKERS} juntos) e agrega um summary",
    )
    ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        metavar="N",
        help="repetições do MESMO juiz (só a parte determinística; persona roda 1x, sobre a run "
             f"mediana). Agrega por mediana; spread_intra > {SPREAD_INTRA_UNSTABLE_THRESHOLD} => o juiz "
             "se abstém. N=1 (default) é o comportamento de sempre. Custo cresce ~N vezes",
    )
    a = ap.parse_args()
    if a.repeats < 1:
        raise SystemExit("--repeats precisa ser >= 1")

    if a.track == "build":
        if a.repeats > 1:
            raise SystemExit("--repeats só existe na trilha result (a trilha build não tem D1-D4 repetíveis)")
        judge_id = a.judge or DEFAULT_BUILD_JUDGE_ID
        if judge_id not in all_build_judge_ids():
            raise SystemExit(f"{judge_id} não é um judge_id da trilha build (judges/registry_build.tsv)")
        if a.all_judges:
            run_all_judges_build(a.dry_run)
            return 0
        run_one(judge_id, a.dry_run, track="build")
        return 0

    judge_id = a.judge or DEFAULT_JUDGE_ID
    if judge_id not in all_judge_ids():
        raise SystemExit(f"{judge_id} não é um judge_id da trilha J1 (judges/registry.tsv)")
    if a.all_judges:
        run_all_judges(a.dry_run, repeats=a.repeats)
        return 0

    run_one(judge_id, a.dry_run, repeats=a.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
