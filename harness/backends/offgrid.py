"""Fallback off-grid: primário indisponível degrada para o tier local em vez de
bloquear o run.

O primário é o `claude_code`. CLI ausente, sem auth ou sem rede é
INDISPONIBILIDADE DO AMBIENTE, não erro da unidade — e parar o trabalho por isso
custa mais que rodar no modelo local, que já está pago. O gate é `[fallback]` do
`config/models.toml` (`offgrid` liga, `tier` diz para onde cair); desligado, o
comportamento é o de sempre (blocked).

Toda degradação é REGISTRADA (`Resolution.degraded`, gravado pelo chamador no
nó `offgrid_fallback` do ledger): run que rodou num tier mais barato sem dizer
falsificaria o prior do router, que é keyed em (kind, tier, backend).

Duas coisas que este módulo NÃO faz: escolher tier por competência (isso é do
router, que não se edita) e falar com LLM — preflight é checagem local por
contrato.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.backends import registry
from harness.backends.base import Backend
from harness.routing import router
from harness.types import Preflight

FALLBACK_SECTION = "fallback"
FALLBACK_TIER = "t0"  # usado só quando `[fallback]` liga o gate e omite o tier
# Nó do ledger que carrega a degradação. Tabela `node_events` aceita payload
# JSON livre, então o registro não custa migração de schema.
LEDGER_NODE = "offgrid_fallback"

# Falha de preflight que NÃO é indisponibilidade: o servidor está de pé e quem
# está errado é o pedido (modelo que ninguém serve, adapter sem base). Degradar
# aqui esconderia config errada da unidade atrás de um run que "funcionou".
CONFIG_MARKERS = ("não está baixado/servido", "não serve o base")


@dataclass(frozen=True)
class Resolution:
    """Quem executa de fato. `backend is None` = blocked, motivo em `preflight`.

    `degraded` vazio é o caminho normal; preenchido, traz o que o chamador
    pretendia rodar antes de o ambiente dizer não.
    """

    name: str
    model: str | None
    preflight: Preflight
    backend: Backend | None = None
    # Tier do fallback, para o chamador não gravar (tier do primário, backend do
    # local) no ledger — par que nunca existiu e sujaria o prior.
    tier: str | None = None
    degraded: Mapping[str, str] | None = None


def resolve_backend(
    name: str,
    model: str | None = None,
    *,
    config_path: Path | str | None = None,
) -> Resolution:
    """Backend pronto para executar, degradando para o tier off-grid quando cabe.

    Backend desconhecido continua levantando `KeyError`: nome errado é erro de
    quem chamou, não indisponibilidade.
    """
    pre, backend = _preflight(name, model)
    if pre.ok:
        return Resolution(name=name, model=model, preflight=pre, backend=backend)

    alvo = _fallback_tier(config_path)
    if alvo is None or not _indisponivel(pre.reason):
        return Resolution(name=name, model=model, preflight=pre)

    alvo_model = alvo.model or None
    if alvo.backend == name and alvo_model == model:
        # Cair em si mesmo não é degradar — o motivo real é o mesmo de antes.
        return Resolution(name=name, model=model, preflight=pre)

    fb_pre, fb = _preflight(alvo.backend, alvo_model)
    if not fb_pre.ok:
        motivo = f"{pre.reason} | fallback {alvo.backend} também indisponível: {fb_pre.reason}"
        return Resolution(name=name, model=model, preflight=Preflight(ok=False, reason=motivo))

    print(
        f"offgrid: {name} indisponível ({pre.reason}) — degradando para "
        f"{alvo.backend} {alvo_model or '-'} (tier {alvo.name})",
        file=sys.stderr,
    )
    return Resolution(
        name=alvo.backend,
        model=alvo_model,
        preflight=fb_pre,
        backend=fb,
        tier=alvo.name,
        degraded={
            "intended_backend": name,
            "intended_model": model or "",
            "reason": pre.reason,
        },
    )


def _preflight(name: str, model: str | None) -> tuple[Preflight, Backend]:
    backend = registry.get_backend(name)
    if model is not None and hasattr(backend, "model"):
        # Backend model-selectable checa o modelo pedido no próprio preflight.
        backend.model = model
    return backend.preflight(), backend


def _fallback_tier(config_path: Path | str | None):
    """Tier do `[fallback]`, ou None quando o gate está desligado.

    Fail-open: este módulo só INFORMA que existe um plano B. models.toml
    ilegível não pode virar exceção nova num caminho que já estava falhando —
    vira uma linha no stderr e nenhum fallback.
    """
    try:
        cfg = router.load_config(config_path)
        section = cfg.get(FALLBACK_SECTION) or {}
        if not section.get("offgrid"):
            return None
        return router.tier_by_name(cfg, str(section.get("tier") or FALLBACK_TIER))
    except (router.RouterError, OSError) as exc:
        print(f"offgrid: fallback desligado — {exc}", file=sys.stderr)
        return None


def _indisponivel(reason: str) -> bool:
    """Preflight é checagem de ambiente por contrato (`base.Backend`), então
    falhar nele é indisponibilidade por default. A exceção são os poucos motivos
    que dizem "o servidor está de pé, o pedido é que está errado"."""
    baixo = reason.lower()
    return not any(m in baixo for m in CONFIG_MARKERS)
