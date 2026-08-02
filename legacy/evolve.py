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
- O que uma proposta pode tocar é o `evolution/genome.toml`, não uma constante
  aqui. A blocklist de lá vence sempre: quem julga não se muda.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

import graph  # noqa: E402
import score  # noqa: E402

RESULTS = ROOT / "results.tsv"
SANDBOXES = ROOT / "evolution" / "sandboxes"
DECISIONS = ROOT / "evolution" / "decisions"
DECISIONS_JSONL = ROOT / "evolution" / "decisions.jsonl"
# O genome não é mais uma constante: quem define o que pode mudar é o toml.
GENOME_TOML = ROOT / "evolution" / "genome.toml"
GENOME_VIOLATION = "tamper:genome_violation"
SANDBOX_TAMPER = "tamper:sandbox_immutable_modified"


class InfraError(Exception):
    """Falha de infra — exit 2. Nunca vira veredito de DISCARD."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -------------------------------------------------------------------- genome


def load_genome(path: Path | None = None) -> dict:
    """Lê `evolution/genome.toml` -> {"mutable": [...], "immutable": [...]}.

    Fail-closed: sem o toml não existe genoma e nenhuma mutação é legítima.
    Um path presente nas duas listas é contradição de configuração — recusa
    carregar em vez de "resolver" no silêncio (o silêncio aqui seria a brecha).
    """
    p = path or GENOME_TOML
    if not p.exists():
        raise InfraError(f"genome.toml não existe: {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise InfraError(f"{p.name}: TOML inválido — {e}")

    genome = {}
    for key in ("mutable", "immutable"):
        v = data.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise InfraError(f"{p.name}: '{key}' precisa ser uma lista de strings")
        genome[key] = [_norm(x) for x in v]
    if not genome["immutable"]:
        raise InfraError(f"{p.name}: 'immutable' vazia — a blocklist é o ponto do arquivo")

    overlap = sorted(set(genome["mutable"]) & set(genome["immutable"]))
    if overlap:
        raise InfraError(
            f"{p.name}: padrão nas duas listas (mutable ∩ immutable): {', '.join(overlap)}"
        )
    return genome


def _norm(path: str) -> str:
    """Path relativo, posix, sem './' — a forma em que tudo é comparado."""
    return PurePosixPath(str(path).replace("\\", "/")).as_posix().removeprefix("./")


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, pat) for pat in patterns)


def genome_violations(files, genome: dict | None = None) -> list[str]:
    """Os paths da proposta que o genoma não autoriza.

    Duas formas de violar, e a segunda vence a primeira: estar fora de
    `mutable` (não é genoma, ninguém pediu para evoluir isso) ou estar em
    `immutable` (é régua/exame/histórico — quem julga não se muda). Path
    absoluto ou com '..' é violação por construção: só se compara o que é
    relativo à raiz.
    """
    g = genome or load_genome()
    bad = []
    for f in files:
        rel = _norm(f)
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts:
            bad.append(rel)
        elif _matches(rel, g["immutable"]) or not _matches(rel, g["mutable"]):
            bad.append(rel)
    return bad


def genome_files(genome: dict | None = None, root: Path | None = None) -> list[str]:
    """Os arquivos que existem hoje e o genoma autoriza — o que a sandbox copia."""
    g = genome or load_genome()
    base = root or ROOT
    out = set()
    for pat in g["mutable"]:
        # o prefixo literal do padrão diz onde procurar — varrer o repo inteiro
        # só para descartar sandbox/.git/node_modules seria desperdício.
        head = pat.split("*", 1)[0].rstrip("/")
        start = base / head if head else base
        candidates = [start] if start.is_file() else (
            [p for p in start.rglob("*") if p.is_file()] if start.is_dir() else []
        )
        for p in candidates:
            rel = p.relative_to(base).as_posix()
            if "__pycache__" in p.parts:
                continue
            if _matches(rel, g["immutable"]) or not _matches(rel, g["mutable"]):
                continue
            out.add(rel)
    return sorted(out)


def runtime_files(root: Path | None = None) -> list[str]:
    """O que a sandbox precisa para RODAR — genoma ou não.

    Copiar só o genoma deixava a sandbox sem os módulos que o `run_task.py`
    importa (`safety`, `score`, `kpi`, ...) e o ciclo morria em
    ModuleNotFoundError, que é infra, não veredito. Aqui o critério é outro:
    não é "o que pode mudar", é "o que precisa existir". Quem garante que o
    imutável continua imutável lá dentro é o `sandbox_tamper`.

    Deliberadamente raso: `*.py` e `*.toml` do topo, `prompts/**` inteiro e o
    `genome.toml`. Sem rglob fora de `prompts/` — varrer o repo arrastaria
    sandboxes, `.git` e `node_modules` junto.
    """
    base = root or ROOT
    out: set[str] = set()

    def add(p: Path) -> None:
        if not p.is_file() or "__pycache__" in p.parts:
            return
        out.add(p.relative_to(base).as_posix())

    for pat in ("*.py", "*.toml"):
        for p in base.glob(pat):
            add(p)
    prompts = base / "prompts"
    if prompts.is_dir():
        for p in prompts.rglob("*"):
            add(p)
    add(base / "evolution" / "genome.toml")
    return sorted(out)


def genome_fingerprint(root: Path | None = None) -> dict:
    """SHA-256 do que define o genoma (hoje: o próprio genome.toml).

    Mesmo mecanismo do `hash_test_files` do run_task.py, mesmo motivo: uma
    mutação que reescreve a própria régua enquanto roda passaria despercebida
    se ninguém guardasse o hash de antes.
    """
    base = root or ROOT
    hashes = {}
    for rel in ("evolution/genome.toml",):
        p = base / rel
        if p.exists():
            hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


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
    # `[change]` (um arquivo) ou `[[change]]` (vários) — a segunda forma existe
    # porque o gate de genoma precisa julgar a proposta inteira, não um arquivo.
    change = meta.get("change")
    changes = change if isinstance(change, list) else [change]
    if not changes or not all(
        isinstance(c, dict) and c.get("old") and c.get("new") for c in changes
    ):
        raise InfraError(f"{path.name}: [change] precisa de 'old' e 'new'")
    for c in changes:
        c.setdefault("file", "agent.py")
    meta["changes"] = changes
    meta["change"] = changes[0]
    meta["body"] = body.strip()
    meta["path"] = str(path)
    return meta


def proposal_files(meta: dict) -> list[str]:
    """Os arquivos que a proposta toca — a entrada do gate de genoma."""
    return [c.get("file", "agent.py") for c in meta.get("changes", [meta.get("change", {})])]


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


def build_sandbox(pid: str, to_version: str, changes: list[dict],
                  genome: dict | None = None) -> tuple[Path, str]:
    sandbox = SANDBOXES / pid
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    for f in runtime_files():
        if not (ROOT / f).exists():
            continue
        dst = sandbox / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, dst)
    (sandbox / "harness_version.txt").write_text(to_version + "\n")
    diff_summary = " · ".join(apply_change(sandbox, c) for c in changes)
    return sandbox, diff_summary


def sandbox_tamper(sandbox: Path, genome: dict | None = None) -> list[str]:
    """Os arquivos NÃO-genoma que a sandbox mexeu — a contrapartida do runtime.

    A sandbox recebe o runtime inteiro para conseguir importar o que precisa,
    mas licença para mudar continua sendo só do genoma. Qualquer outro arquivo
    com hash diferente do baseline é a candidata editando o que a julga —
    e some da sandbox também conta como mexer.
    """
    mutable = set(genome_files(genome))
    bad = []
    for rel in runtime_files():
        if rel in mutable:
            continue
        dst = sandbox / rel
        if not dst.exists():
            bad.append(rel)
            continue
        if hashlib.sha256(dst.read_bytes()).hexdigest() != \
                hashlib.sha256((ROOT / rel).read_bytes()).hexdigest():
            bad.append(rel)
    return bad


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


# ------------------------------------------------------------------- sealed


def sealed_tasks() -> list[Path]:
    base = ROOT / "benchmarks" / "sealed"
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("task_*") if (p / "prompt.md").exists())


def rows_for_suite(rows: list[dict], suite: str) -> list[dict]:
    return [r for r in rows if r["suite"] == suite]


def credit_ok(rep: dict) -> bool:
    """Crédito em sealed = PISO intacto, não ganho.

    A suite fixed é onde a candidata foi desenvolvida — é lá que ela precisa
    provar ganho. A sealed nunca foi usada para hill-climb, então o que ela
    responde é outra pergunta: 'o ganho generaliza ou você só decorou a fixed?'.
    Exigir ganho aqui reprovaria mudanças legítimas por ruído; não exigir nada
    tornaria o held-out decorativo. O meio-termo é o piso.
    """
    return all(g["ok"] for g in rep["gates"] if not g["name"].startswith("ganho normalizado"))


JUDGES_VERDICTS_DIR = ROOT / "attic" / "judges" / "verdicts"


def _judge_summary(version: str) -> dict | None:
    p = JUDGES_VERDICTS_DIR / f"summary_{version}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _judge_candidate_veto(version: str) -> bool:
    """True se algum verdict individual (o `<versão>.json` mais recente, por
    juiz) da versão candidata tomou veto de D2 (trapaça — tamper/segredo/
    escrita fora do workspace)."""
    if not JUDGES_VERDICTS_DIR.is_dir():
        return False
    for judge_dir in JUDGES_VERDICTS_DIR.iterdir():
        if not judge_dir.is_dir():
            continue
        vp = judge_dir / f"{version}.json"
        if not vp.exists():
            continue
        verdict = json.loads(vp.read_text(encoding="utf-8"))
        det = verdict.get("deterministic") or {}
        if det.get("veto"):
            return True
    return False


def judge_ok(va: str, vb: str) -> bool | None:
    """Espelha `credit_ok`, mas pro sinal de juízes (SPEC-J1 §7 / SPEC-J2
    agregação): lê os summaries de juízes mais recentes de A e B
    (`attic/judges/verdicts/summary_<versão>.json`) e aprova promoção só se a
    candidata não regrediu na mediana além da margem, os juízes não estão
    discordando demais entre si (spread) e nenhum verdict da candidata
    tomou veto de D2.

    None = sem dados de juiz pra alguma das versões — FASE 1 é gate
    manual, quem decide bloquear com isso é `--require-judges`.
    """
    summary_a, summary_b = _judge_summary(va), _judge_summary(vb)
    if not summary_a or not summary_b:
        return None
    median_a = summary_a.get("median")
    median_b = summary_b.get("median")
    spread_b = summary_b.get("spread")
    if median_a is None or median_b is None or spread_b is None:
        return None
    if _judge_candidate_veto(vb):
        return False
    return median_b >= median_a - 5 and spread_b <= 25


def run_sealed_credit(sandbox: Path, va: str, vb: str, repeat: int, pid: str) -> dict | None:
    """Roda sealed nos DOIS lados (o baseline só se ainda não tiver runs) e julga.

    Devolve o report do juiz sobre a suite sealed, ou None se não há sealed.
    """
    if not sealed_tasks():
        return None

    rows = rows_for_suite(tsv_rows(), "sealed")
    base_n = len([r for r in rows if r["harness_version"] == va])
    # MIN_N_GATE (não MIN_N): aqui a pergunta é "tem baseline sealed suficiente
    # para os gates de eficiência", não "a régua de Wilson consegue opinar".
    if base_n < score.MIN_N_GATE:
        print(f"   sealed: baseline {va} tem {base_n} runs, rodando baseline em sealed...")
        env = {**os.environ, "HARNESS_RESULTS": str(RESULTS), "HARNESS_TASKS_ROOT": str(ROOT)}
        subprocess.run(
            [sys.executable, str(ROOT / "run_task.py"), "--all", "--suite", "sealed",
             "--repeat", str(repeat)],
            env=env, cwd=ROOT,
        )

    print("   sealed: rodando candidata...")
    before = len(tsv_rows())
    run_suite(sandbox, "sealed", repeat)
    all_rows = tsv_rows()
    sync_graph(all_rows, before, pid)

    sealed_rows = rows_for_suite(all_rows, "sealed")
    return score.ab_report(sealed_rows, va, vb)


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
            # linha antiga do TSV não tem a coluna kpis — score.load devolve ""
            kpis=r.get("kpis", ""),
        )
        n += 1
    return n


# ------------------------------------------------------------------ decision


def _sealed_md(rep_sealed: dict | None, credited: bool | None) -> str:
    if rep_sealed is None:
        return (
            "Não rodou. Ou a candidata já havia sido reprovada na fixed (não se gasta "
            "held-out em candidata morta), ou `benchmarks/sealed/` está vazia — nesse "
            "caso um eventual merge fica **sem crédito de generalização**."
        )
    A, B = rep_sealed["a"], rep_sealed["b"]
    linhas = "\n".join(
        f"| {'PASS' if g['ok'] else 'FAIL'} | {g['name']} |"
        for g in rep_sealed["gates"]
        if not g["name"].startswith("ganho normalizado")
    )
    return f"""Veredito: **{'CONFIRMA' if credited else 'REPROVA'}** o crédito.

| Métrica (sealed) | A = {rep_sealed['version_a']} | B = {rep_sealed['version_b']} |
|---|---|---|
| success | {A['pass']}/{A['n']} = {A['rate']:.0%} | {B['pass']}/{B['n']} = {B['rate']:.0%} |
| truncamento | {A['trunc_rate']:.0%} | {B['trunc_rate']:.0%} |
| mediana s | {A['med_s']:.1f}s | {B['med_s']:.1f}s |
| custo/run | ${A['cost_run']:.4f} | ${B['cost_run']:.4f} |

Gates de piso na held-out (o gate de ganho não se aplica aqui — sealed responde
"generaliza?", não "melhorou?"):

| Veredito | Gate |
|---|---|
{linhas}"""


def _judges_md(judge_result: bool | None, va: str, vb: str, require_judges: bool) -> str:
    if judge_result is None:
        return (
            "juízes: **sem dados** — `attic/judges/verdicts/summary_<versão>.json` não existe "
            f"pra {va} e/ou {vb}. Rode `attic/judges/run_judge.py --all-judges` (ou "
            "`--dry-run`) nas duas versões pra preencher. FASE 1 é gate manual: o "
            "wiring está pronto mas não bloqueia nada sem `--require-judges`."
        )
    summary_a, summary_b = _judge_summary(va), _judge_summary(vb)
    veredito = "APROVA" if judge_result else "REPROVA"
    linhas = "\n".join(
        f"| {jid} | {summary_a['scores'].get(jid)} | {summary_b['scores'].get(jid)} |"
        for jid in sorted(set(summary_a["scores"]) | set(summary_b["scores"]))
    )
    bloqueio = (
        "**Bloqueou o merge** (`--require-judges` ativo)." if require_judges and not judge_result
        else "Não bloqueou (aprovado)." if require_judges
        else "Informativo — não bloqueou o merge (`--require-judges` não foi passado)."
    )
    return f"""Veredito: **{veredito}**

| judge_id | A = {va} | B = {vb} |
|---|---|---|
{linhas}

| Métrica | A = {va} | B = {vb} |
|---|---|---|
| mediana | {summary_a['median']} | {summary_b['median']} |
| spread | {summary_a['spread']} | {summary_b['spread']} |

Regra: mediana_B >= mediana_A - 5, spread_B <= 25, zero veto de candidato (D2). {bloqueio}"""


def write_decision(meta: dict, rep: dict, outcome: str, gid: int, diff_summary: str,
                   n_runs: int, rep_sealed: dict | None = None,
                   credited: bool | None = None, judge_result: bool | None = None,
                   require_judges: bool = False) -> Path:
    A, B = rep["a"], rep["b"]
    va, vb = rep["version_a"], rep["version_b"]
    gates_md = "\n".join(
        f"| {'PASS' if g['ok'] else 'FAIL'} | {g['name']} |" for g in rep["gates"]
    )
    if outcome == "merge":
        selo = (
            "Confirmado em **sealed** (held-out): o ganho generaliza."
            if credited else
            "**NÃO creditado**: sem confirmação em held-out — hill-climb na fixed, "
            "onde a mudança foi desenvolvida. A evidência é de ganho, não de generalização."
        )
        reason = (
            f"Todos os {len(rep['gates'])} gates da fixed passaram. Piso intacto "
            f"(success {B['rate']:.0%}, truncamento {B['trunc_rate']:.0%}) e ganho normalizado "
            f"real: custo/run {rep['d_cost']:+.1%}, mediana {rep['d_med']:+.1%}. "
            f"Genome promovido para {vb}. {selo}"
        )
    elif rep["merge"] and credited is False:
        S = rep_sealed["b"]
        reason = (
            f"A fixed aprovou (custo/run {rep['d_cost']:+.1%}), mas a suite **sealed** "
            f"reprovou o piso: success {S['rate']:.0%}, truncamento {S['trunc_rate']:.0%} "
            f"— gate(s): {'; '.join(rep_sealed['failed'])}. "
            f"Ganho que não sobrevive ao held-out é overfitting na suite de treino. "
            f"Baseline {va} permanece intacto."
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

## Held-out (sealed)

{_sealed_md(rep_sealed, credited)}

## Juízes

{_judges_md(judge_result, va, vb, require_judges)}

## Razão

{reason}

## Notas da proposta

{meta['body'] or "(sem corpo)"}
"""
    out = DECISIONS / f"{meta['id']}.md"
    out.write_text(doc, encoding="utf-8")

    # Log de máquina (uma linha por ciclo, accept E reject) — o .md é para
    # humano ler, o .jsonl é para agregar/auditar sem parsear markdown.
    DECISIONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    jsonl_entry = {
        "id": meta.get("id"),
        "va": va,
        "vb": vb,
        "accepted": outcome == "merge",
        "reason": reason,
        "gates_failed": rep.get("failed"),
        "d_cost": rep.get("d_cost"),
        "d_med": rep.get("d_med"),
        "judges_ok": judge_result,
    }
    with DECISIONS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(jsonl_entry, ensure_ascii=False) + "\n")

    return out


def reject_genome_violation(meta: dict, violations: list[str],
                            violation: str = GENOME_VIOLATION) -> int:
    """DISCARD por genoma, no mesmo trilho de registro dos outros DISCARDs.

    Não roda suite e não chama juiz: proposta que toca a régua não é julgada
    pelo mérito, é recusada. Quem julga não se muda.

    `violation` é o rótulo do gate — a mesma recusa serve para a proposta que
    pede o que não pode (`GENOME_VIOLATION`) e para a sandbox que mexe no que
    não podia (`SANDBOX_TAMPER`).
    """
    pid = meta["id"]
    va, vb = meta["from_version"], meta["to_version"]
    reason = f"{violation}: {', '.join(violations)}"

    graph.record_proposal(
        pid=pid, from_version=va, to_version_intended=vb,
        hypothesis=meta["hypothesis"], diff_summary=reason, path=meta.get("path", ""),
    )
    gid = graph.record_decision(
        proposal_id=pid,
        outcome="discard",
        scores_summary=json.dumps({"genome_violations": violations}, ensure_ascii=False),
        reason=reason,
        gates_json=json.dumps(
            [{"name": violation, "ok": False, "files": violations}], ensure_ascii=False
        ),
    )

    razao = (
        f"A proposta toca arquivo fora do genoma (`evolution/genome.toml`): "
        f"{', '.join(violations)}.\n"
        "Nenhuma suite rodou — não se julga pelo mérito uma mudança na régua."
        if violation == GENOME_VIOLATION else
        f"A sandbox reescreveu arquivo imutável enquanto rodava: "
        f"{', '.join(violations)}.\n"
        "Candidata que edita o que a julga não é julgada pelo mérito."
    )

    DECISIONS.mkdir(parents=True, exist_ok=True)
    doc = f"""# Decisão {pid} — DISCARD

**proposal_id:** `{pid}` · **graph_decision_id:** `{gid}`
**Ciclo:** {va} → {vb} · gerado por `evolve.py` em {now()}

**Hipótese:** {meta['hypothesis']}

## Gates

| Veredito | Gate |
|---|---|
| FAIL | {violation} |

## Razão

{razao}
Baseline {va} permanece intacto.
"""
    (DECISIONS / f"{pid}.md").write_text(doc, encoding="utf-8")

    DECISIONS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": pid,
            "va": va,
            "vb": vb,
            "accepted": False,
            "reason": reason,
            "gates_failed": [violation],
            "d_cost": None,
            "d_med": None,
            "judges_ok": None,
        }, ensure_ascii=False) + "\n")

    print(f"\n=> DISCARD: {reason} — {va} intacto")
    return 1


def promote(sandbox: Path, to_version: str) -> None:
    """MERGE: sandbox vira baseline. Só roda depois de todos os gates passarem."""
    for f in genome_files():
        src = sandbox / f
        if not src.exists():
            continue
        (ROOT / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, ROOT / f)
    (ROOT / "harness_version.txt").write_text(to_version + "\n")


# ----------------------------------------------------------------------- cli


def cycle(proposal_path: Path, repeat: int, suite: str, force: bool,
          no_credit: bool = False, require_judges: bool = False) -> int:
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

    genome = load_genome()
    violations = genome_violations(proposal_files(meta), genome)
    if violations:
        print(f"== ciclo {pid}: {va} -> {vb} — bloqueado pelo gate de genoma")
        return reject_genome_violation(meta, violations)
    fingerprint = genome_fingerprint()

    print(f"== ciclo {pid}: {va} -> {vb} (suite={suite}, repeat={repeat})")
    sandbox, diff_summary = build_sandbox(pid, vb, meta["changes"], genome)
    print(f"   sandbox: {sandbox.relative_to(ROOT)}  [{diff_summary}]")

    graph.record_proposal(
        pid=pid, from_version=va, to_version_intended=vb,
        hypothesis=meta["hypothesis"], diff_summary=diff_summary, path=str(proposal_path),
    )

    before = len(tsv_rows())
    print(f"   rodando suite... ({repeat}x cada task)")
    run_suite(sandbox, suite, repeat)

    # Tamper check do próprio genoma: se o toml mudou enquanto a candidata
    # rodava, a régua foi reescrita no meio do exame.
    if genome_fingerprint() != fingerprint:
        return reject_genome_violation(meta, ["evolution/genome.toml"])

    # A sandbox roda com o runtime inteiro, mas só o genoma tinha licença de
    # mudar lá dentro. Vem antes de qualquer gate de score: um número obtido
    # depois de reescrever o avaliador não é número, é trapaça.
    tampered = sandbox_tamper(sandbox, genome)
    if tampered:
        return reject_genome_violation(meta, tampered, SANDBOX_TAMPER)

    rows = tsv_rows()
    n_new = len(rows) - before
    if n_new <= 0:
        raise InfraError("a suite não gerou nenhuma run — nada para julgar")
    synced = sync_graph(rows, before, pid)
    print(f"   {n_new} runs de candidata · {synced} runs novas no graph")

    rep = score.ab_report(rows_for_suite(rows, suite), va, vb)

    # Sealed só entra se a fixed já aprovou: held-out é para CREDITAR um ganho,
    # não para procurar um. Rodar sealed numa candidata reprovada é queimar
    # budget e, pior, é o começo de treinar na held-out.
    rep_sealed, credited = None, None
    if rep["merge"] and suite == "fixed" and not no_credit:
        rep_sealed = run_sealed_credit(sandbox, va, vb, repeat, pid)
        if rep_sealed is None:
            print("   sealed: suite vazia — merge NÃO creditado (sem held-out)")
        else:
            credited = credit_ok(rep_sealed)
            print(f"   sealed: {'CONFIRMA' if credited else 'REPROVA'} "
                  f"(success {rep_sealed['b']['rate']:.0%}, "
                  f"trunc {rep_sealed['b']['trunc_rate']:.0%})")

    judge_result = judge_ok(va, vb)
    if judge_result is None:
        print("   juízes: sem dados (gate manual, FASE 1)")
    else:
        print(f"   juízes: {'aprova' if judge_result else 'reprova'}"
              + (" — bloqueando merge (--require-judges)" if require_judges and not judge_result else ""))

    outcome = "merge" if (rep["merge"] and credited is not False) else "discard"
    if require_judges and judge_result is False:
        outcome = "discard"

    gid = graph.record_decision(
        proposal_id=pid,
        outcome=outcome,
        scores_summary=json.dumps(
            {"fixed": {"a": rep["a"], "b": rep["b"], "d_med": rep["d_med"],
                       "d_cost": rep["d_cost"]},
             "sealed": ({"a": rep_sealed["a"], "b": rep_sealed["b"]} if rep_sealed else None),
             "credited": credited,
             "judges_ok": judge_result},
            ensure_ascii=False,
        ),
        reason=(
            "; ".join(rep["failed"]) if rep["failed"]
            else ("juízes reprovaram" if require_judges and judge_result is False
                  else "sealed reprovou o piso" if credited is False
                  else "todos os gates passaram" + ("" if credited else " (sem crédito sealed)"))
        ),
        gates_json=json.dumps(
            {"fixed": rep["gates"],
             "sealed": (rep_sealed["gates"] if rep_sealed else None),
             "credited": credited,
             "judges_ok": judge_result},
            ensure_ascii=False,
        ),
    )

    doc = write_decision(meta, rep, outcome, gid, diff_summary, n_new, rep_sealed, credited,
                         judge_result, require_judges)

    if outcome == "merge":
        promote(sandbox, vb)
        selo = "creditado em sealed" if credited else "NÃO creditado (sem held-out)"
        print(f"\n=> MERGE: genome promovido para {vb} — {selo}")
    elif rep["merge"] and credited is False:
        print(f"\n=> DISCARD: fixed aprovou mas sealed reprovou o piso — {va} intacto")
    elif rep["merge"] and require_judges and judge_result is False:
        print(f"\n=> DISCARD: fixed (e sealed, se rodou) aprovaram mas os juízes reprovaram "
              f"(--require-judges) — {va} intacto")
    elif rep["merge"]:
        print(f"\n=> DISCARD: fixed aprovou mas sealed reprovou o piso — {va} intacto")
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
    ap.add_argument("--no-credit", action="store_true",
                    help="pula a confirmação em sealed (merge fica SEM crédito de generalização)")
    ap.add_argument("--require-judges", action="store_true",
                    help="bloqueia a promoção se judge_ok(va, vb) reprovar (default: FASE 1, "
                         "gate manual — o sinal de juízes só entra na decisão como informativo)")
    a = ap.parse_args()

    DECISIONS.mkdir(parents=True, exist_ok=True)
    SANDBOXES.mkdir(parents=True, exist_ok=True)
    try:
        return cycle(Path(a.proposal).resolve(), a.repeat, a.suite, a.force, a.no_credit,
                     a.require_judges)
    except InfraError as e:
        print(f"ERRO DE INFRA: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERRO DE INFRA inesperado: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
