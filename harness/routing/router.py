"""Router — qual CLASSE DE CUSTO paga esta unidade. Zero chamada de LLM.

`tier` é preço, `kind` é natureza do trabalho: ortogonais de propósito. Três
regras, nesta ordem, cada uma registrada em `reasons`:

    base      tier inicial do kind (`[router.kind]` em config/models.toml) —
              hipótese de partida, não verdade
    prior     Wilson lower de (kind, tier, backend) abaixo de `prior_floor`
              com amostra >= `min_n` => sobe um tier. A chave tem os TRÊS
              campos de propósito: o router velho colapsava classe e tier
              (`history_prior(rows, task_class, task_class)`) e por isso era
              incapaz de aprender "o tier t0 é ruim pro kind code" — ou
              condenava o tier inteiro, ou não aprendia nada.
    attempt   cada tentativa que voltou pra fila sobe um tier (clampa no topo)

Config quebrada derruba o load: um router que cai em default silencioso quando
o TOML sumiu esconde o bug e paga a conta no tier errado.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from harness.routing import config_dir
from harness.routing._stats import wilson_lower_bound
from harness.routing.kinds import VALID_KINDS, classify_kind
from harness.types import RunRow, Selection, UnitSpec, Verdict

MODELS_FILE = "models.toml"


class RouterError(Exception):
    """models.toml inválido/ausente."""


@dataclass(frozen=True)
class Tier:
    """Uma classe de custo. `model` vazio = o backend escolhe o dele (backend
    cujo CLI fixa o próprio modelo ignoraria o que fosse pinado aqui)."""

    name: str
    backend: str
    model: str
    max_turns: int
    cost_rank: int


def models_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else config_dir() / MODELS_FILE


def load_config(path: Path | str | None = None) -> dict:
    p = models_path(path)
    try:
        cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise RouterError(f"models.toml ilegível: {p} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise RouterError(f"models.toml inválido: {p} ({e})") from e

    raw = cfg.get("tier") or []
    if not raw:
        raise RouterError("models.toml sem nenhum [[tier]]")
    ranks = [int(t.get("cost_rank", -1)) for t in raw]
    if sorted(ranks) != list(range(len(ranks))):
        raise RouterError(f"cost_rank precisa ser único e contíguo a partir de 0: {sorted(ranks)}")
    names = [t.get("name", "") for t in raw]
    if "" in names or len(set(names)) != len(names):
        raise RouterError(f"nomes de tier vazios ou duplicados: {names}")
    for t in raw:
        if not t.get("backend"):
            raise RouterError(f"tier {t['name']!r} sem backend")
        if int(t.get("max_turns", 0)) < 1:
            raise RouterError(f"tier {t['name']!r}: max_turns precisa ser >= 1")

    router = cfg.get("router") or {}
    if router.get("default_tier") not in names:
        raise RouterError(f"default_tier {router.get('default_tier')!r} não é um tier: {names}")
    if int(router.get("max_attempts", 0)) < 1:
        raise RouterError("max_attempts precisa ser >= 1")
    if int(router.get("min_n", 0)) < 1:
        raise RouterError("min_n precisa ser >= 1")
    floor = float(router.get("prior_floor", 0))
    if not 0 < floor <= 1:
        raise RouterError(f"exige 0 < prior_floor <= 1 (got {floor})")
    for kind, tier_name in router.get("kind", {}).items():
        if kind not in VALID_KINDS:
            raise RouterError(f"[router.kind].{kind} não é um Kind: {sorted(VALID_KINDS)}")
        if tier_name not in names:
            raise RouterError(f"[router.kind].{kind} = {tier_name!r} não é um tier: {names}")
    return cfg


def tiers(cfg: dict) -> list[Tier]:
    return sorted(
        (
            Tier(t["name"], t["backend"], str(t.get("model", "")), int(t["max_turns"]),
                 int(t["cost_rank"]))
            for t in cfg["tier"]
        ),
        key=lambda t: t.cost_rank,
    )


def tier_by_name(cfg: dict, name: str) -> Tier:
    for t in tiers(cfg):
        if t.name == name:
            return t
    raise RouterError(f"tier desconhecido: {name!r}")


def tier_by_rank(cfg: dict, rank: int) -> Tier:
    """Clampa nas pontas: pedir acima do topo devolve o topo, não estoura."""
    ts = tiers(cfg)
    return ts[max(0, min(rank, len(ts) - 1))]


def _succ_n(history: Sequence[RunRow], kind: str, tier: Tier) -> tuple[int, int]:
    """Amostra do prior. A chave é (kind, tier, backend) — trocar qualquer um
    dos três é outra população, não a mesma com ruído."""
    succ = n = 0
    for r in history:
        if r.kind != kind or r.tier != tier.name or r.backend != tier.backend:
            continue
        n += 1
        succ += 1 if r.ok else 0
    return succ, n


def select(
    unit: UnitSpec,
    history: Sequence[RunRow],
    attempt: int = 0,
    cfg: dict | None = None,
) -> Selection:
    cfg = load_config() if cfg is None else cfg
    router = cfg["router"]
    kind, reasons = classify_kind(unit)

    cur = tier_by_name(cfg, router.get("kind", {}).get(kind, router["default_tier"]))
    reasons.append(f"base:{kind}->{cur.name}")

    # Sobe enquanto o tier corrente tiver amostra suficiente e piso ruim. O
    # bump é iterativo porque é a MESMA regra aplicada de novo: parar no
    # primeiro degrau quando o de cima também tem histórico ruim seria
    # arbitrário. Teto natural = número de tiers.
    floor, min_n = float(router["prior_floor"]), int(router["min_n"])
    for _ in tiers(cfg):
        succ, n = _succ_n(history, kind, cur)
        if n < min_n:
            break
        lower = wilson_lower_bound(succ, n)
        if lower >= floor:
            break
        up = tier_by_rank(cfg, cur.cost_rank + 1)
        if up.name == cur.name:  # já é o topo: não há pra onde subir
            reasons.append(f"prior_top:{cur.name}(n={n},lb={lower:.2f})")
            break
        reasons.append(f"prior_bump:{cur.name}->{up.name}(n={n},lb={lower:.2f})")
        cur = up

    attempt = max(0, int(attempt))
    if attempt:
        escalated = tier_by_rank(cfg, cur.cost_rank + attempt)
        reasons.append(f"attempt+{attempt}:{cur.name}->{escalated.name}")
        cur = escalated

    return Selection(
        backend=cur.backend,
        model=cur.model,
        tier=cur.name,
        kind=kind,
        max_turns=cur.max_turns,
        reasons=tuple(reasons),
    )


def should_escalate(sel: Selection, verdict: Verdict, attempt: int, cfg: dict) -> bool:
    """Escala se a régua reprovou, ainda existe tier acima e ainda cabe uma
    tentativa no orçamento (`max_attempts` inclui a primeira).

    Tamper não é assunto daqui — quem barra é o gate: o agente que quebrou a
    regra não falhou por falta de modelo, e pagar um modelo melhor pro mesmo
    prompt é pagar mais caro pelo mesmo ataque.
    """
    if verdict.passed:
        return False
    if int(attempt) + 1 >= int(cfg["router"]["max_attempts"]):
        return False
    return tier_by_name(cfg, sel.tier).cost_rank < tiers(cfg)[-1].cost_rank
