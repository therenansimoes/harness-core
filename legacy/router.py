#!/usr/bin/env python3
"""router.py — escolha determinística de modelo por unidade de trabalho (D7).

A pergunta que isto responde é "qual tier paga esta task", e a resposta NÃO
pode custar uma chamada de LLM: um classificador que custa mais que o haiku
que ele escolheria é um roteador que se paga negativo. Então é scoring puro
sobre o metadado que já existe (prompt, verify, notes), com três correções:

    score    soma de pesos de sinais (`[[signal]]` em evolution/models.toml)
    prior    histórico do projeto por (classe de task, tier) via Wilson lower
             (mesma régua do score.py — 6/6 não é "100%", é [0.61, 1.0])
    attempt  cada falha que volta pra fila sobe um rank

Nada aqui é opinião do momento: pesos, faixas e palavras são config; os
`kind` de sinal são código (KINDS abaixo) porque são o que precisa de teste.
Kind desconhecido no TOML derruba o load — sinal que não existe virando peso
zero silencioso é pior que crash.

    python3 router.py --project website-faz-rogers [--unit 0001] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import score  # noqa: E402

ROOT = Path(__file__).parent.resolve()
MODELS_TOML = ROOT / "evolution" / "models.toml"

# token que parece nome de arquivo ("Base.astro", "src/pages/index.astro").
_FILE_RE = re.compile(r"[\w./-]+\.[A-Za-z]{2,5}\b")


class RouterError(Exception):
    """Config de roteamento inválida/ausente. Nunca cai em default silencioso:
    um router que erra pro sonnet quando o TOML sumiu esconde o bug e paga a
    conta."""


@dataclass(frozen=True)
class Tier:
    name: str
    rank: int
    model: str
    max_turns: int
    est_cost_per_run: float


@dataclass(frozen=True)
class Selection:
    tier: Tier
    task_class: str
    score: int
    reasons: list[str] = field(default_factory=list)
    attempt: int = 0
    escalated_from: str | None = None


# ------------------------------------------------------------------- config


def load_models(path: Path | str | None = None) -> dict:
    p = Path(path) if path else MODELS_TOML
    try:
        cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise RouterError(f"models.toml ilegível: {p} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise RouterError(f"models.toml inválido: {p} ({e})") from e

    raw_tiers = cfg.get("tier") or []
    if not raw_tiers:
        raise RouterError("models.toml sem nenhum [[tier]]")
    ranks = [int(t.get("rank", -1)) for t in raw_tiers]
    if sorted(ranks) != list(range(len(ranks))):
        raise RouterError(f"ranks precisam ser únicos e contíguos a partir de 0: {sorted(ranks)}")
    names = [t.get("name", "") for t in raw_tiers]
    if len(set(names)) != len(names) or "" in names:
        raise RouterError(f"nomes de tier vazios ou duplicados: {names}")

    if cfg.get("default_tier") not in names:
        raise RouterError(f"default_tier {cfg.get('default_tier')!r} não é um tier: {names}")
    if int(cfg.get("max_attempts", 0)) < 1:
        raise RouterError("max_attempts precisa ser >= 1")
    floor, ceiling = float(cfg.get("prior_floor", 0)), float(cfg.get("prior_ceiling", 0))
    if not (0 < floor <= ceiling <= 1):
        raise RouterError(f"exige 0 < prior_floor <= prior_ceiling <= 1 (got {floor}, {ceiling})")

    top = max(raw_tiers, key=lambda t: int(t["rank"]))["name"]
    for key in cfg.get("thresholds", {}):
        if key not in names:
            raise RouterError(f"[thresholds].{key} não é um tier: {names}")
        if key == top:
            raise RouterError(f"[thresholds].{key} é o tier de maior rank — ele é o 'resto', não uma faixa")

    for sig in cfg.get("signal", []):
        if sig.get("kind") not in KINDS:
            raise RouterError(f"signal {sig.get('name')!r}: kind {sig.get('kind')!r} desconhecido")
        if not sig.get("name"):
            raise RouterError("signal sem name")
    return cfg


def tiers(cfg: dict) -> list[Tier]:
    return sorted(
        (
            Tier(t["name"], int(t["rank"]), t["model"], int(t["max_turns"]), float(t["est_cost_per_run"]))
            for t in cfg["tier"]
        ),
        key=lambda t: t.rank,
    )


def tier_by_name(cfg: dict, name: str) -> Tier:
    for t in tiers(cfg):
        if t.name == name:
            return t
    raise RouterError(f"tier desconhecido: {name!r}")


def tier_by_rank(cfg: dict, rank: int) -> Tier:
    ts = tiers(cfg)
    return ts[max(0, min(rank, len(ts) - 1))]


# ------------------------------------------------------------------ scoring


def task_features(prompt: str, verify_src: str = "", notes: str = "") -> dict:
    """Métricas cruas da task. `text` é o palheiro (prompt+notes, minúsculo)
    usado pelos kinds cujo padrão vem do TOML (keyword_any/regex_count_gt) —
    por isso o dict não é só de números."""
    prompt = prompt or ""
    text = f"{prompt}\n{notes or ''}"
    return {
        "prompt_chars": float(len(prompt)),
        "files_mentioned": float(len(set(_FILE_RE.findall(prompt)))),
        "verify_lines": float(len((verify_src or "").splitlines())),
        "text": text.lower(),
    }


def _kw_hit(text: str, words: list[str]) -> bool:
    """Casa no INÍCIO da palavra, não no meio: "refator" pega "refatorar" mas
    "texto" não pega "contexto"."""
    return any(re.search(r"(?<![0-9a-zà-ú])" + re.escape(w.lower()), text) for w in words)


KINDS = {
    "prompt_chars_gt": lambda f, s: f["prompt_chars"] > float(s["value"]),
    "files_mentioned_gt": lambda f, s: f["files_mentioned"] > float(s["value"]),
    "verify_lines_gt": lambda f, s: f["verify_lines"] > float(s["value"]),
    "regex_count_gt": lambda f, s: len(re.findall(s["pattern"], f["text"])) > int(s["value"]),
    "keyword_any": lambda f, s: _kw_hit(f["text"], s.get("words", [])),
}


def score_task(feats: dict, cfg: dict) -> tuple[int, list[str]]:
    total, reasons = 0, []
    for sig in cfg.get("signal", []):
        if KINDS[sig["kind"]](feats, sig):
            w = int(sig["weight"])
            total += w
            reasons.append(f"{sig['name']}{w:+d}")
    return total, reasons


def classify(score_value: int, cfg: dict) -> str:
    thresholds = cfg.get("thresholds", {})
    for t in tiers(cfg):
        if t.name in thresholds and score_value <= int(thresholds[t.name]):
            return t.name
    return tiers(cfg)[-1].name


# -------------------------------------------------------------------- prior


def _succ_n(rows: list[dict], task_class: str, tier_name: str) -> tuple[int, int]:
    succ = n = 0
    for r in rows:
        notes = r.get("notes") or ""
        if f"class:{task_class}" not in notes or f"tier:{tier_name}" not in notes:
            continue
        n += 1
        succ += 1 if str(r.get("success", "")).strip() == "1" else 0
    return succ, n


def history_prior(rows: list[dict], task_class: str, tier_name: str, cfg: dict) -> tuple[str, str | None]:
    """Corrige o tier pelo histórico do projeto. Sobe se o tier corrente vem
    falhando nessa classe; desce se o tier de baixo vem dando conta. Nunca os
    dois — e nunca com amostra menor que min_n (Wilson não opina no vazio)."""
    rows = rows or []
    min_n = int(cfg.get("min_n", score.MIN_N))
    cur = tier_by_name(cfg, tier_name)

    succ, n = _succ_n(rows, task_class, tier_name)
    if n >= min_n and score.wilson_interval(succ, n)[0] < float(cfg["prior_floor"]):
        up = tier_by_rank(cfg, cur.rank + 1)
        if up.name != cur.name:
            return up.name, "prior_bump+1"

    if cur.rank > 0:
        down = tier_by_rank(cfg, cur.rank - 1)
        dsucc, dn = _succ_n(rows, task_class, down.name)
        if dn >= min_n and score.wilson_interval(dsucc, dn)[0] >= float(cfg["prior_ceiling"]):
            return down.name, "prior_demote-1"

    return cur.name, None


# ------------------------------------------------------------------- seleção


def select(prompt: str, verify_src: str = "", notes: str = "", attempt: int = 0,
           rows: list[dict] | None = None, cfg: dict | None = None) -> Selection:
    cfg = cfg or load_models()
    feats = task_features(prompt, verify_src, notes)
    sc, reasons = score_task(feats, cfg)
    task_class = classify(sc, cfg)  # chave estável do histórico: ANTES do prior

    tier_name, prior_reason = history_prior(rows or [], task_class, task_class, cfg)
    if prior_reason:
        reasons.append(prior_reason)

    base_rank = tier_by_name(cfg, tier_name).rank
    attempt = max(0, int(attempt))
    if attempt:
        reasons.append(f"attempt+{attempt}")
    tier = tier_by_rank(cfg, base_rank + attempt)
    return Selection(
        tier=tier, task_class=task_class, score=sc, reasons=reasons,
        attempt=attempt, escalated_from=tier_name if attempt else None,
    )


def should_escalate(notes: str, attempt: int, sel: Selection, cfg: dict) -> bool:
    """Tamper não escala: o agente não falhou por falta de modelo, ele quebrou
    a regra — dar um modelo melhor pro mesmo prompt é pagar mais pelo mesmo
    ataque."""
    if "tamper:" in (notes or ""):
        return False
    if int(attempt) + 1 >= int(cfg["max_attempts"]):
        return False
    return sel.tier.rank < tiers(cfg)[-1].rank


def env_for(sel: Selection) -> dict[str, str]:
    return {"HARNESS_MODEL": sel.tier.model, "HARNESS_MAX_TURNS": str(sel.tier.max_turns)}


# ----------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mostra o tier que o router escolheria por unidade")
    ap.add_argument("--project", required=True)
    ap.add_argument("--unit", default=None, help="só esta unidade (id)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import project  # tardio: project.py importa router (e lê env no import)

    proj_dir = project.PROJECTS_ROOT / args.project
    if not proj_dir.is_dir():
        print(f"projeto não encontrado: {proj_dir}", file=sys.stderr)
        return 2
    cfg = load_models()
    rows = project._rows_of(proj_dir)

    out = []
    for r in project.read_queue(proj_dir):
        if args.unit and r["id"] != args.unit:
            continue
        prompt_path = proj_dir / (r.get("prompt_file") or "")
        verify_path = proj_dir / (r.get("verify") or "")
        prompt = prompt_path.read_text(errors="replace") if prompt_path.is_file() else ""
        verify_src = verify_path.read_text(errors="replace") if verify_path.is_file() else ""
        sel = select(prompt, verify_src, r.get("notes", ""),
                     attempt=int(r.get("attempts") or 0), rows=rows, cfg=cfg)
        out.append({"id": r["id"], "tier": sel.tier.name, "score": sel.score,
                    "class": sel.task_class, "reasons": sel.reasons})

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for o in out:
            print(f"{o['id']}\t{o['tier']}\t{o['score']}\t{o['class']}\t{','.join(o['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
