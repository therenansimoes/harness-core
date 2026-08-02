#!/usr/bin/env python3
"""runplan.py — o que EXATAMENTE vai para uma run, montado antes de chamar o agente.

Hoje o agente lê o próprio genoma de constantes de módulo (agent.ALLOWED_TOOLS,
agent.SYSTEM_PROMPT) e do env (HARNESS_MODEL/HARNESS_MAX_TURNS). Isso é uma
fonte de verdade implícita e global: dois kinds de task no mesmo processo não
conseguem pedir ferramentas diferentes, e ninguém consegue LOGAR o que foi
enviado sem reimplementar a mesma leitura.

RunPlan é esse pacote explícito e imutável. Nesta fase ele reproduz byte a byte
o comportamento atual (system prompt do agent, tools do [default] de
evolution/tools.toml == agent.ALLOWED_TOOLS); skills e memory_digest são as
camadas seguintes e ficam vazias.

A régua de tools é dupla: evolution/tools.toml é MUTÁVEL (uma proposta pode
mexer), safety.ALLOWED_TOOLS_MAX é IMUTÁVEL. Pedir tool fora do teto derruba o
load — mesmo contrato do router.RouterError: nunca filtrar em silêncio, porque
um plano que silenciosamente perde uma tool vira run que falha por motivo
errado.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import safety  # noqa: E402

ROOT = Path(__file__).parent.resolve()
TOOLS_TOML = ROOT / "evolution" / "tools.toml"

DEFAULT_KIND = "default"


class RunPlanError(Exception):
    """Config de plano inválida/ausente, ou plano pedindo tool fora do teto de
    safety. Nunca cai em default silencioso."""


@dataclass(frozen=True)
class RunPlan:
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    mcp_config_path: str | None = None
    max_turns: int = 0
    skills: list[str] = field(default_factory=list)
    memory_digest: str = ""


# --------------------------------------------------------------------- config


def load_tools(path: Path | str | None = None) -> dict:
    p = Path(path) if path else TOOLS_TOML
    try:
        cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise RunPlanError(f"tools.toml ilegível: {p} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise RunPlanError(f"tools.toml inválido: {p} ({e})") from e

    if DEFAULT_KIND not in cfg:
        raise RunPlanError(f"tools.toml sem seção [{DEFAULT_KIND}]: {p}")

    for kind, section in cfg.items():
        if not isinstance(section, dict) or "tools" not in section:
            raise RunPlanError(f"[{kind}] sem chave tools: {p}")
        tools = section["tools"]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise RunPlanError(f"[{kind}].tools precisa ser lista de strings: {p}")
        try:
            safety.validate_tools(tools)
        except safety.SafetyViolation as e:
            raise RunPlanError(f"[{kind}] em {p}: {e}") from e
    return cfg


def tools_for(kind: str, cfg: dict | None = None) -> list[str]:
    """Tools da seção [<kind>], caindo em [default] quando o kind não existe —
    fallback é sobre kind DESCONHECIDO, não sobre config quebrada (essa levanta)."""
    cfg = cfg if cfg is not None else load_tools()
    section = cfg.get(kind) or cfg[DEFAULT_KIND]
    return list(section["tools"])


# ----------------------------------------------------------------------- build


def build(kind: str, tier, workspace: Path, project: str) -> RunPlan:
    """Monta o plano de uma unidade. `tier` é um router.Tier (só max_turns é
    usado aqui; o modelo continua indo por env, via router.env_for)."""
    import agent  # tardio: agent NÃO importa runplan, e a ordem inversa fecharia ciclo

    return RunPlan(
        system_prompt=agent._system_prompt(workspace),
        tools=tools_for(kind),
        mcp_config_path=None,
        max_turns=int(tier.max_turns),
        skills=[],
        memory_digest="",
    )
