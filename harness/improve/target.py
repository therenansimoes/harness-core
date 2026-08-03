"""Alvo do próximo ciclo: qual mutação de config tem o maior ganho esperado.

    ganho = freq(padrão de falha) x custo_médio(padrão) x prior(regra)

Os três fatores existem para matar um viés cada: `freq` impede otimizar o caso
raro, `custo_médio` impede otimizar a falha barata, `prior` impede insistir na
regra que já foi reprovada. Ganho abaixo de `min_gain` não vale o experimento —
`pick_target` devolve None, e None NÃO é "tente qualquer coisa": é o risco 5 da
SPEC, sem gradiente novo o loop escala pro humano em vez de inventar mutação.

Regra é declarativa (arquivo + chave + de/para), nunca `patch_fn`. Uma função
de patch teria que morar em código, e código que o loop escolhe aplicar é o
loop escrevendo código — o genoma existe justamente para que a auto-melhoria
seja calibração de `config/*.toml`, não edição de si mesma.

Padrão de falha = o `exit_reason` da linha do ledger, cortado no primeiro ':'
(`verify_failed:exit=1` e `verify_failed:exit=2` são a mesma doença).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from harness.improve import catalog_path
from harness.ruler.wilson import MIN_N
from harness.types import RunRow

# Veredito do genoma, não da régua: a regra nem chegou a virar experimento.
# Mora aqui (e não no grafo) porque quem monta a fila precisa dele.
REJECTED = "REJECTED"

# Experimento que começou e não terminou (deadline, erro, humano abortou). Não
# é veredito da régua nem parede do genoma: é amostra que não existe. Fica no
# ledger porque replay precisa saber que a mutação FOI aplicada um dia — mas
# não conta como tentativa no prior (`with_ledger_priors` só olha KEEP e
# INCONCLUSIVE): punir a regra por um deadline seria aprender com o relógio.
ABORTED = "ABORTED"

# Defaults do `[improve]`. Ficam aqui e não só no toml porque o catálogo é
# mutável no genoma: arquivo sem a chave não pode significar "sem limite".
DEFAULTS: dict[str, float] = {
    # Preço do segundo desperdiçado. Existe porque modelo local reporta
    # `cost_usd = 0.0` e sem isto TODA falha local teria custo zero — o loop
    # nunca acharia gradiente numa máquina que roda de graça.
    "sec_cost_usd": 0.0001,
    "min_gain": 1e-6,      # abaixo disto o experimento custa mais que a cura
    "min_fail_n": 1,       # falhas mínimas do padrão para ele existir
    "n_per_arm": 6,        # tentativas por braço do A/B (MIN_N da régua)
    "window": 200,         # runs recentes que contam como evidência
    "max_parallel": 1,     # teto do fan-out; 1 = sequencial (ver autopilot_graph)
}


class CatalogError(Exception):
    """catalog.toml ausente ou malformado."""


@dataclass(frozen=True)
class Rule:
    """Uma mutação CANDIDATA: trocar `key` de `from_value` para `to_value` em
    `target_file`. `fails_on` são os padrões de falha que a regra promete
    atacar — sem isso não há como estimar ganho, só palpite.

    `prior_succ`/`prior_n` são o histórico da própria regra (quantas vezes ela
    já foi KEEP em quantas avaliações). Partem do toml e são atualizados pelo
    ledger de mutações antes do pick.
    """

    id: str
    target_file: str
    key: str
    from_value: Any
    to_value: Any
    hypothesis: str = ""
    fails_on: tuple[str, ...] = ()
    prior_succ: int = 0
    prior_n: int = 0

    def prior(self) -> float:
        """Laplace, não Wilson: `wilson_lower_bound(0, 0)` é 0.0 e zeraria o
        ganho de toda regra nova — o loop nunca sairia do lugar. Regra sem
        histórico vale 0.5 (ignorância honesta), KEEP sobe, DISCARD desce.
        A régua Wilson julga o experimento TERMINADO; aqui só se ordena fila.
        """
        return (self.prior_succ + 1) / (self.prior_n + 2)


@dataclass(frozen=True)
class Target:
    """A regra escolhida e a conta que a escolheu — `reasons` no mesmo espírito
    do router: decisão sem rastro não é auditável."""

    rule: Rule
    pattern: str
    freq: float
    avg_cost: float
    prior: float
    gain: float
    reasons: tuple[str, ...] = ()


def load_catalog(
    path: Path | str | None = None, root: Path | str | None = None
) -> tuple[list[Rule], dict[str, float]]:
    """Lê `config/catalog.toml` e devolve `(regras, knobs do [improve])`.

    Fail-closed como o genoma: catálogo ilegível não vira catálogo vazio, senão
    o loop confundiria "não tenho regra" com "não sei ler minhas regras".
    """
    p = Path(path) if path is not None else catalog_path(root)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise CatalogError(f"catalog.toml ilegível: {p} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise CatalogError(f"catalog.toml inválido: {p} ({e})") from e

    cfg = dict(DEFAULTS)
    for key, value in (data.get("improve") or {}).items():
        if key not in DEFAULTS:
            raise CatalogError(f"{p}: [improve].{key} não é um knob conhecido")
        cfg[key] = float(value)
    if cfg["n_per_arm"] < MIN_N:
        # Braço menor que o MIN_N da régua produz INCONCLUSIVE por construção:
        # o loop gastaria runs para nunca decidir nada. Fail-closed em vez de
        # rodar um experimento que já se sabe estéril.
        raise CatalogError(
            f"{p}: [improve].n_per_arm = {cfg['n_per_arm']:g} < MIN_N = {MIN_N} — "
            "braço menor que isso nunca deixa a régua opinar"
        )

    rules = [_rule(p, i, raw) for i, raw in enumerate(data.get("rule") or [])]
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise CatalogError(f"{p}: id de regra duplicado: {sorted(ids)}")
    return rules, cfg


def _rule(p: Path, i: int, raw: dict) -> Rule:
    missing = [k for k in ("id", "target_file", "key", "from", "to") if k not in raw]
    if missing:
        raise CatalogError(f"{p}: [[rule]] #{i}: campos faltando: {', '.join(missing)}")
    if raw["from"] == raw["to"]:
        raise CatalogError(f"{p}: regra {raw['id']!r}: 'from' e 'to' são iguais")
    return Rule(
        id=str(raw["id"]),
        target_file=str(raw["target_file"]),
        key=str(raw["key"]),
        from_value=raw["from"],
        to_value=raw["to"],
        hypothesis=str(raw.get("hypothesis", "")),
        fails_on=tuple(str(x) for x in raw.get("fails_on", ())),
        prior_succ=int(raw.get("prior_succ", 0)),
        prior_n=int(raw.get("prior_n", 0)),
    )


def failure_pattern(exit_reason: str) -> str:
    """`kpi_regression:sec_build` -> `kpi_regression`."""
    return exit_reason.partition(":")[0]


def waste(row: RunRow, sec_cost_usd: float) -> float:
    """Recurso queimado por uma run. Dólar quando o backend reporta, mais o
    tempo sempre: run local é grátis em dinheiro e cara em relógio."""
    return (row.cost_usd or 0.0) + row.sec_total * sec_cost_usd


def failure_stats(
    history: Sequence[RunRow], sec_cost_usd: float
) -> dict[str, tuple[int, float]]:
    """`padrão -> (nº de falhas, custo médio da falha)`. Só linhas com ok=False:
    o que interessa é o desperdício, não o custo de trabalhar."""
    buckets: dict[str, list[float]] = {}
    for row in history:
        if row.ok:
            continue
        buckets.setdefault(failure_pattern(row.exit_reason), []).append(
            waste(row, sec_cost_usd)
        )
    return {k: (len(v), sum(v) / len(v)) for k, v in buckets.items()}


def pick_target(
    history: Sequence[RunRow],
    catalog: Iterable[Rule],
    cfg: dict[str, float] | None = None,
) -> Target | None:
    """A regra de maior ganho esperado, ou None quando nada vale a pena.

    Empate desempata por `rule.id` — dois ciclos com a mesma evidência têm que
    escolher a mesma regra, senão o A/B mede ruído de ordenação.
    """
    knobs = {**DEFAULTS, **(cfg or {})}
    rows = list(history)[: int(knobs["window"])]
    if not rows:
        return None

    stats = failure_stats(rows, knobs["sec_cost_usd"])
    total = len(rows)
    min_fail_n = int(knobs["min_fail_n"])

    best: list[Target] = []
    for rule in catalog:
        candidate = _score(rule, stats, total, min_fail_n)
        if candidate is not None and candidate.gain >= knobs["min_gain"]:
            best.append(candidate)
    if not best:
        return None
    return min(best, key=lambda t: (-t.gain, t.rule.id))


def _score(
    rule: Rule, stats: dict[str, tuple[int, float]], total: int, min_fail_n: int
) -> Target | None:
    """Melhor padrão que a regra ataca. Regra que ataca vários fica com o pior
    deles: é o que ela tem mais a ganhar, e é o número que o humano cobra."""
    scored: list[tuple[float, str, int, float]] = []
    for pattern in rule.fails_on:
        hit = stats.get(pattern)
        if hit is None or hit[0] < min_fail_n:
            continue
        n, cost = hit
        scored.append((n / total * cost, pattern, n, cost))
    if not scored:
        return None
    # desempate pelo nome do padrão: dois padrões de mesmo peso não podem
    # depender da ordem em que o toml os listou.
    weight, pattern, n, cost = max(scored, key=lambda s: (s[0], s[1]))
    prior = rule.prior()
    return Target(
        rule=rule,
        pattern=pattern,
        freq=n / total,
        avg_cost=cost,
        prior=prior,
        gain=weight * prior,
        reasons=(
            f"pattern:{pattern}({n}/{total})",
            f"cost:{cost:.6f}",
            f"prior:{rule.prior_succ}/{rule.prior_n}->{prior:.2f}",
        ),
    )


def with_ledger_priors(catalog: Iterable[Rule], mutations: Iterable) -> list[Rule]:
    """Soma ao prior do toml o que o ledger de mutações já sabe da regra.

    Sai da fila de vez a regra já DISCARDada pela régua ou REJECTED pelo genoma:
    a primeira só re-derivaria o mesmo veredito com a mesma evidência, a segunda
    voltaria a bater na mesma parede — nos dois casos é orçamento queimado.

    INCONCLUSIVE volta, mas conta como tentativa: soma em `prior_n` sem somar em
    `prior_succ`. É o que faz o prior DECAIR a cada empate — sem isso a regra
    reapareceria com o mesmo ganho para sempre e o loop repetiria o experimento
    eternamente em vez de cair em NO_GRADIENT e chamar o humano.
    """
    seen: dict[str, list[str]] = {}
    for m in mutations:
        seen.setdefault(m.rule_id, []).append(m.verdict)

    out: list[Rule] = []
    for rule in catalog:
        verdicts = seen.get(rule.id, [])
        if any(v in ("DISCARD", REJECTED) for v in verdicts):
            continue
        tried = [v for v in verdicts if v in ("KEEP", "INCONCLUSIVE")]
        out.append(
            replace(
                rule,
                prior_succ=rule.prior_succ + tried.count("KEEP"),
                prior_n=rule.prior_n + len(tried),
            )
        )
    return out


# --- ações de auto-evolução -----------------------------------------------------
# A mutação de config é a ação nativa (pick_target + mutate); outras ações
# (ex.: research) entram por aqui, no espírito de backends/registry: builtin
# por import tardio + registro manual para teste/plugin. O autopilot seleciona
# pela chave em `actions()`.


@dataclass(frozen=True)
class Action:
    """Uma ação que o loop sabe propor e aplicar. Assinaturas livres de
    propósito: cada ação tem a própria proposta, o contrato comum é o par."""

    name: str
    propose: Any
    apply: Any


_manual_actions: dict[str, Action] = {}


def register_action(action: Action) -> None:
    _manual_actions[action.name] = action


def unregister_action(name: str) -> None:
    _manual_actions.pop(name, None)


def actions() -> dict[str, Action]:
    # Import tardio como nos backends builtin: quem nunca consulta ações não
    # paga o import de research (que puxa o registry de backends).
    from harness.improve import research

    out: dict[str, Action] = {research.ACTION: research.action()}
    out.update(_manual_actions)
    return out


def get_action(name: str) -> Action:
    found = actions()
    if name not in found:
        raise KeyError(
            f"ação desconhecida: {name!r} (disponíveis: {', '.join(sorted(found))})"
        )
    return found[name]
