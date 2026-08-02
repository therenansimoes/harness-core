"""Kind — o QUE a unidade é. Rótulo ORTOGONAL ao custo: quem escolhe preço é o
router; aqui só se diz de que natureza é o trabalho.

Classificação determinística, sem LLM: um classificador que custa mais que o
modelo que ele escolheria se paga negativo. Sinais são metadado que já existe —
extensão dos paths citados no prompt, nome de arquivo sem extensão
(`Dockerfile`) e palavras-chave. As regras são config (`config/kinds.toml`); a
PRECEDÊNCIA é código, porque é o que precisa de teste:

    1. `unit.kind` explícito  -> vence tudo, o classificador nem roda
    2. maior score            -> keyword pesa mais que extensão
    3. empate                 -> primeiro em `[precedence].order`
    4. nenhum sinal           -> `[precedence].fallback`
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import get_args

from harness.routing import config_dir
from harness.types import Kind, UnitSpec

KINDS_FILE = "kinds.toml"
VALID_KINDS = frozenset(get_args(Kind))
RULE_KEYS = frozenset({"extensions", "keywords", "filenames"})
WEIGHT_DEFAULTS = {"keyword": 2.0, "extension": 1.0, "filename": 1.0}

# token que parece caminho de arquivo ("Base.astro", "src/pages/index.astro").
_FILE_RE = re.compile(r"[\w./-]+\.[a-z0-9]{1,6}\b")


class KindError(Exception):
    """kinds.toml inválido/ausente. Nunca cai em default silencioso: um
    classificador que despenca pro fallback quando o TOML sumiu esconde o bug
    e manda toda unidade pro mesmo tier."""


def kinds_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else config_dir() / KINDS_FILE


def load_kinds(path: Path | str | None = None) -> dict:
    """Lê e valida. Regra desconhecida derruba o load — chave com typo virando
    sinal silenciosamente nulo é pior que crash."""
    p = kinds_path(path)
    try:
        cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise KindError(f"kinds.toml ilegível: {p} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise KindError(f"kinds.toml inválido: {p} ({e})") from e

    declared = cfg.get("kind") or {}
    if not declared:
        raise KindError("kinds.toml sem nenhum [kind.*]")
    for name, rules in declared.items():
        if name not in VALID_KINDS:
            raise KindError(f"[kind.{name}] não é um Kind: {sorted(VALID_KINDS)}")
        unknown = set(rules) - RULE_KEYS
        if unknown:
            raise KindError(f"[kind.{name}]: regras desconhecidas {sorted(unknown)}")
        bad_ext = [e for e in rules.get("extensions", []) if not e.startswith(".")]
        if bad_ext:
            raise KindError(f"[kind.{name}]: extensões sem ponto {bad_ext}")

    prec = cfg.get("precedence") or {}
    order = prec.get("order") or []
    if set(order) != set(declared):
        raise KindError(
            f"[precedence].order precisa listar exatamente os kinds declarados: "
            f"{sorted(order)} != {sorted(declared)}"
        )
    if prec.get("fallback") not in declared:
        raise KindError(f"[precedence].fallback {prec.get('fallback')!r} não é um kind declarado")

    unknown_w = set(cfg.get("weights", {})) - set(WEIGHT_DEFAULTS)
    if unknown_w:
        raise KindError(f"[weights]: pesos desconhecidos {sorted(unknown_w)}")
    return cfg


def weights(cfg: dict) -> dict[str, float]:
    return {k: float(cfg.get("weights", {}).get(k, v)) for k, v in WEIGHT_DEFAULTS.items()}


def _word_hit(text: str, word: str) -> bool:
    """Casa no INÍCIO da palavra, não no meio: "refator" pega "refatorar" mas
    "texto" não pega "contexto"."""
    return re.search(r"(?<![0-9a-zà-ú])" + re.escape(word.lower()), text) is not None


def _extensions(text: str) -> set[str]:
    return {suf for suf in (Path(t).suffix for t in _FILE_RE.findall(text)) if suf}


def classify_kind(unit: UnitSpec, cfg: dict | None = None) -> tuple[Kind, list[str]]:
    """Devolve o kind e a lista de regras que o produziram — `reasons` é o que
    torna a escolha auditável depois, no ledger."""
    cfg = load_kinds() if cfg is None else cfg
    if unit.kind:
        if unit.kind not in VALID_KINDS:
            raise KindError(f"unit {unit.id}: kind {unit.kind!r} não existe")
        return unit.kind, [f"explicit:{unit.kind}"]

    text = (unit.prompt or "").lower()
    exts = _extensions(text)
    w = weights(cfg)
    order: list[str] = cfg["precedence"]["order"]
    scores: dict[str, float] = {}
    reasons: list[str] = []

    for kind in order:  # ordem do TOML não importa: a de [precedence] manda
        rules = cfg["kind"][kind]
        total = 0.0
        for word in rules.get("keywords", []):
            if _word_hit(text, word):
                total += w["keyword"]
                reasons.append(f"{kind}:kw:{word}")
        for ext in rules.get("extensions", []):
            if ext.lower() in exts:
                total += w["extension"]
                reasons.append(f"{kind}:ext:{ext}")
        for name in rules.get("filenames", []):
            if _word_hit(text, name):
                total += w["filename"]
                reasons.append(f"{kind}:file:{name}")
        scores[kind] = total

    best = max(scores.values())
    if best <= 0:
        fallback = cfg["precedence"]["fallback"]
        return fallback, [f"fallback:{fallback}"]

    winners = [k for k in order if scores[k] == best]
    chosen = winners[0]
    if len(winners) > 1:
        reasons.append(f"tiebreak:precedence:{chosen}")
    reasons.append(f"kind:{chosen}:score={best:g}")
    return chosen, reasons
