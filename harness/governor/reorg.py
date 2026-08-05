"""Reorg: quando o errado é a TOPOLOGIA do run, não o modelo que paga por ele.

O governor aperta prazo, custo e foco de uma topologia FIXA. Aqui a pergunta é
outra: o desenho do run está certo? Quatro regras, todas sobre sinal que já está
no ledger:

    escalate_route      a mesma classe de falha voltou N vezes -> subir um tier
                        não é "tentar de novo", é trocar quem paga
    insert_reviewer     falha concentrada numa área -> a topologia pede um
                        revisor naquele ponto
    collapse_fleet      gasto passou do valor da tarefa -> a frota encolhe
    skip_orchestration  tarefa trivial -> orquestrar custa mais que fazer

Só DUAS delas têm efeito no runtime (`effect="applied"`): o delta de tier do
`escalate_route` e o freio do `skip_orchestration`. As outras duas são decisões
GRAVADAS (`effect="recorded"`) — inserir um nó num grafo vivo e variar o tamanho
da frota exigiriam mudanças que ninguém verificou ainda, e decisão anotada com
honestidade vale mais que efeito inventado. O ledger é o mesmo dos dois jeitos,
então quando o efeito existir a série histórica já estará lá.

Núcleo PURO: `decide` e `diff_active` não leem disco nem relógio — o sinal entra
pronto em `ReorgSignals`. A camada impura mora no fim do arquivo, isolada, e é a
única que fala com o ledger.

Tudo falha ABERTO, como o resto do governor: config ausente ou torta degrada
para os defaults congelados aqui, sinal ilegível vira zero, e nenhuma função
daqui levanta por dado ruim. Reorg quebrado nunca derruba um run.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.routing import config_dir

GOVERNOR_TOML = "governor.toml"
SECTION = "reorg"

# Nó do ledger onde as decisões ficam (uma linha por decisão, ver `run_graph`).
NODE = "reorg"

# `effect`: o que o runtime FEZ com a decisão. Vocabulário fechado de propósito —
# "recorded" é a confissão de que a regra decidiu e ninguém aplicou.
APPLIED = "applied"
RECORDED = "recorded"

# `state`: o ciclo de vida da decisão no ledger (ativa até o sinal sumir).
STATE_ACTIVE = "applied"
STATE_REVERTED = "reverted"

R_ESCALATE = "escalate_route"
R_REVIEWER = "insert_reviewer"
R_COLLAPSE = "collapse_fleet"
R_SKIP = "skip_orchestration"


@dataclass(frozen=True)
class ReorgConfig:
    """Defaults congelados — o comportamento de fábrica sem `[reorg]` no toml.

    `enabled=True` com limiares folgados: as duas regras que mexem no runtime
    exigem evidência repetida (`repeat_failures`) ou tarefa declaradamente
    trivial, então ligado de fábrica não muda run nenhum sem motivo no ledger."""

    enabled: bool = True
    repeat_failures: int = 2
    area_ratio: float = 0.5
    area_min_n: int = 4
    cost_value_ratio: float = 1.0
    trivial_max_chars: int = 280
    trivial_kinds: tuple[str, ...] = ("config", "content")


@dataclass(frozen=True)
class ReorgSignals:
    """O que o ledger sabe no instante da decisão. Injetado, nunca lido aqui."""

    failure_classes: Mapping[str, int]
    area_counts: Mapping[str, int]
    total_runs: int
    spent_usd: float
    task_value_usd: float
    prompt_chars: int
    kind: str
    attempt: int


@dataclass(frozen=True)
class ReorgDecision:
    """Uma decisão de topologia. `signal` é a evidência que a justificou —
    decisão sem o número que a causou não é auditável nem reversível."""

    rule_id: str
    action: str
    reason: str
    signal: dict = field(default_factory=dict)
    tier_delta: int = 0
    escalate_blocked: bool = False
    effect: str = RECORDED


def _bool(raw: Any, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _pos_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def _frac(raw: Any, default: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if 0.0 <= v <= 1.0 else default


def _pos_float(raw: Any, default: float) -> float:
    if isinstance(raw, bool):
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _kinds(raw: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """Lista de kinds triviais. Item não-string é descartado; lista vazia ou
    torta cai no default (lista vazia desligaria a regra em silêncio)."""
    if not isinstance(raw, (list, tuple)):
        return default
    out = tuple(k for k in raw if isinstance(k, str) and k)
    return out or default


def load_reorg(path: Path | None = None) -> ReorgConfig:
    """`[reorg]` de `config/governor.toml` -> ReorgConfig. Falha aberta campo a
    campo: arquivo ausente, ilegível, sem a seção ou com tipo torto = defaults."""
    p = Path(path) if path is not None else config_dir() / GOVERNOR_TOML
    base = ReorgConfig()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return base
    sec = data.get(SECTION)
    if not isinstance(sec, dict):
        return base
    return ReorgConfig(
        enabled=_bool(sec.get("enabled"), base.enabled),
        repeat_failures=_pos_int(sec.get("repeat_failures"), base.repeat_failures),
        area_ratio=_frac(sec.get("area_ratio"), base.area_ratio),
        area_min_n=_pos_int(sec.get("area_min_n"), base.area_min_n),
        cost_value_ratio=_pos_float(sec.get("cost_value_ratio"), base.cost_value_ratio),
        trivial_max_chars=_pos_int(sec.get("trivial_max_chars"), base.trivial_max_chars),
        trivial_kinds=_kinds(sec.get("trivial_kinds"), base.trivial_kinds),
    )


def _num(raw: Any, default: float = 0.0) -> float:
    if isinstance(raw, bool):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _counts(raw: Any) -> dict[str, int]:
    """Contagem legível de um Mapping torto. Chave/valor ruim some, não levanta."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or isinstance(v, bool):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[k] = n
    return out


def _top(counts: Mapping[str, int]) -> tuple[str, int]:
    """(chave mais frequente, contagem). Empate desempata pelo nome: decisão
    determinística vale mais que a ordem em que o sqlite devolveu as linhas."""
    if not counts:
        return "", 0
    name = min(counts, key=lambda k: (-counts[k], k))
    return name, counts[name]


def decide(sig: ReorgSignals, cfg: ReorgConfig) -> list[ReorgDecision]:
    """As quatro regras sobre o sinal corrente. Nunca levanta, nunca lê nada.

    Precedência: `skip_orchestration` VENCE `escalate_route` — tarefa trivial
    que falhou repetido não fica cara, fica simples; pagar tier de cima pelo
    mesmo prompt de 3 linhas é o anti-padrão que a regra existe para cortar.
    As duas regras log-only são independentes e sempre acumulam: elas não
    disputam o mesmo efeito com ninguém.
    """
    if not cfg.enabled:
        return []

    out: list[ReorgDecision] = []
    failures = _counts(sig.failure_classes)
    areas = _counts(sig.area_counts)
    total = max(0, int(_num(sig.total_runs)))
    chars = max(0, int(_num(sig.prompt_chars)))
    kind = sig.kind if isinstance(sig.kind, str) else ""

    trivial = chars <= cfg.trivial_max_chars and kind in cfg.trivial_kinds
    if trivial:
        out.append(
            ReorgDecision(
                rule_id=R_SKIP,
                action="skip_orchestration",
                reason=f"tarefa trivial ({chars} chars, kind={kind}): orquestrar custa mais",
                signal={"prompt_chars": chars, "kind": kind},
                tier_delta=-1,
                escalate_blocked=True,
                effect=APPLIED,
            )
        )
    else:
        cls, n = _top(failures)
        if n >= cfg.repeat_failures:
            out.append(
                ReorgDecision(
                    rule_id=R_ESCALATE,
                    action="escalate_route",
                    reason=f"classe de falha {cls!r} repetiu {n}x: outro tier paga",
                    signal={"failure_class": cls, "count": n},
                    tier_delta=1,
                    effect=APPLIED,
                )
            )

    area, an = _top(areas)
    if total >= cfg.area_min_n and total and an / total >= cfg.area_ratio:
        out.append(
            ReorgDecision(
                rule_id=R_REVIEWER,
                action="insert_reviewer",
                reason=f"{an}/{total} dos runs caem em {area!r}: área pede revisor",
                signal={"area": area, "count": an, "total": total},
            )
        )

    value = _num(sig.task_value_usd)
    spent = _num(sig.spent_usd)
    # Valor da tarefa desconhecido (<= 0) NÃO condena ninguém: sem denominador a
    # comparação diria "gastou demais" a cada centavo. Fail-open igual ao teto
    # de gasto do governor, que também não corta sem `cost_cap_usd`.
    if value > 0 and spent > value * cfg.cost_value_ratio:
        out.append(
            ReorgDecision(
                rule_id=R_COLLAPSE,
                action="collapse_fleet",
                reason=f"gasto ${spent:.4f} passou do valor ${value:.4f} da tarefa",
                signal={"spent_usd": spent, "task_value_usd": value, "roles_cap": 1},
            )
        )
    return out


def diff_active(
    prev: Sequence[dict], now: Sequence[ReorgDecision]
) -> tuple[list[ReorgDecision], list[dict]]:
    """(decisões novas, decisões revertidas) entre o ledger e o sinal de agora.

    "Ativa" é o ÚLTIMO estado gravado da regra, não qualquer `applied` no
    histórico: regra que já foi revertida não seria revertida de novo a cada
    passagem. Revertida = estava ativa e o sinal que a justificava sumiu —
    decisão de topologia sem sinal vivo é dívida, não decisão."""
    state: dict[str, dict] = {}
    for p in prev:
        if not isinstance(p, Mapping):
            continue
        rule_id = p.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            continue
        state[rule_id] = dict(p)
    active = {k: v for k, v in state.items() if v.get("state", STATE_ACTIVE) == STATE_ACTIVE}

    seen = {d.rule_id for d in now}
    novas = [d for d in now if d.rule_id not in active]
    revertidas = [v for k, v in active.items() if k not in seen]
    return novas, revertidas


# --- camada impura: a única que fala com o ledger ------------------------------


def signals_from_ledger(
    store_mod: Any,
    *,
    project: str | None,
    kind: str | None,
    backend: str | None,
    prompt_chars: int,
    attempt: int,
    spent_usd: float,
    task_value_usd: float,
    limit: int = 50,
    path: Path | None = None,
) -> ReorgSignals:
    """Janela recente do ledger -> `ReorgSignals`.

    Classe de falha = `exit_reason` das runs que NÃO passaram (a run que deu
    certo não tem classe); área = `unit_id`, a unidade de trabalho é o pedaço do
    mundo mais fino que o ledger conhece. Ledger ilegível vira janela vazia:
    sinal ausente é "não decide nada", nunca uma exceção subindo pro grafo.
    """
    try:
        rows = store_mod.history(
            project=project, kind=kind, backend=backend, limit=limit, path=path
        )
    except Exception:
        rows = []

    failures: dict[str, int] = {}
    areas: dict[str, int] = {}
    for r in rows:
        area = getattr(r, "unit_id", None)
        if isinstance(area, str) and area:
            areas[area] = areas.get(area, 0) + 1
        if getattr(r, "ok", True):
            continue
        cls = getattr(r, "exit_reason", None)
        cls = cls if isinstance(cls, str) and cls else "desconhecido"
        failures[cls] = failures.get(cls, 0) + 1

    return ReorgSignals(
        failure_classes=failures,
        area_counts=areas,
        total_runs=len(rows),
        spent_usd=_num(spent_usd),
        task_value_usd=_num(task_value_usd),
        prompt_chars=max(0, int(_num(prompt_chars))),
        kind=kind or "",
        attempt=max(0, int(_num(attempt))),
    )


def active_from_ledger(store_mod: Any, run_id: str, path: Path | None = None) -> list[dict]:
    """Payloads de decisão já gravados PARA ESTE RUN, na ordem de gravação.

    O `run_id` viaja dentro do payload (o nó guarda uma linha por decisão), e é
    por ele que se filtra: decisão de topologia é do run que a tomou.
    """
    try:
        payloads = store_mod.node_payloads(NODE, path)
    except Exception:
        return []
    return [p for p in payloads if isinstance(p, Mapping) and p.get("run_id") == run_id]
