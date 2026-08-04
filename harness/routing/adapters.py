"""Frota de adapters LoRA — qual PESO especializado paga esta unidade.

Ortogonal a tier (preço) e a kind (natureza do trabalho): o adapter é o mesmo
modelo base com um `.safetensors` de LoRA por cima, servido pelo runtime LOCAL.
Trocar de adapter custa um reload (~1,2s medido no mlx_lm.server); reusar o que
já está quente custa zero. Por isso a escolha é determinística e barata — sem
LLM, sem embedding: o mesmo matcher das skills (`harness/skills/loader.py`).

Três regras, nesta ordem:

    explícito   `unit.adapter` manda e o matcher nem roda
    path        adapter com `paths` cujo glob casa um arquivo da unidade
    ranking     tokens de `match` em comum com o prompt da unidade

Ninguém pontuou => `None`, que é o MODELO BASE PURO. Não achar adapter não é
erro: a frota é opt-in e a base sempre atende.

Config quebrada, essa sim, derruba o load (doutrina de `router.load_config`) —
registry com typo virando "sem adapter" em silêncio esconde o bug e paga a conta
com o peso errado. Arquivo AUSENTE é diferente de arquivo torto: sem
`adapters.toml` não existe frota, e isso é o default de quem nunca treinou nada.

Campos consumidos hoje: `id`, `runtime`, `served_model`, `ref`, `kinds`,
`match`, `paths`, `priority` (aqui) e `system`, `enable_thinking`,
`temperature`, `top_p`, `repeat_penalty`, `max_tokens` (no backend, ver
`deepagents_backend._build_agent` e `._model_for`). `output` e `scale` são
metadado do registro — validados e carregados, ainda não consumidos por ninguém.

`tool_format` é o formato de tool calling em que o peso foi treinado, e hoje só
"native" existe de verdade. O porquê, medido contra o mlx_lm.server instalado
(0.29, `server.py` + `tokenizer_utils.py`), está em VALID_TOOL_FORMATS.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from harness.routing import config_dir
from harness.routing.kinds import VALID_KINDS

# Matcher emprestado das skills de propósito: dois rankings independentes com a
# mesma promessa divergiriam no primeiro ajuste de stopword. `_path_hit` só lê
# `.paths` do que recebe, então `Adapter` serve no lugar de `Skill`.
from harness.skills.loader import _path_hit, _tokens
from harness.types import UnitSpec

ADAPTERS_FILE = "adapters.toml"

# `mlx` = mlx_lm.server (adapter por request, no corpo do /v1/chat/completions);
# `lmstudio` = o servidor da porta 1234, que carrega o peso do lado dele.
VALID_RUNTIMES = frozenset({"mlx", "lmstudio"})
VALID_OUTPUTS = frozenset({"json", "csv", "text", "html"})

# Formato de tool calling do peso. "native" = o do chat template do BASE, o
# único caminho que fecha o ciclo hoje. "hermes" (tools declaradas em <tools> no
# system, resposta em <tool_call>{json}</tool_call>) está no vocabulário porque
# existe peso treinado assim — mas o registro REJEITA no load, e isso é
# deliberado. O que a sondagem do mlx_lm.server instalado mostrou:
#
#   * prompt   `chat_template_kwargs` do corpo do request vira **kwargs do
#     `apply_chat_template`, então dá até para mandar um template inteiro por
#     request. O lado de IDA é resolvível.
#   * resposta NÃO é. O parser de tool call sai de `_infer_tool_parser(base
#     chat_template)` no LOAD do modelo (`tokenizer_utils.load`), e o tokenizer
#     vem do BASE — o `adapter_path` só carrega PESO. Sem `<tool_call>` no
#     template do base, `tokenizer.has_tool_calling` é False, a máquina de
#     estados de tool call nem é armada (`server.py`, `if tokenizer.
#     has_tool_calling`) e o `<tool_call>` que o adapter gerar volta como
#     `content` cru, sem o array `tool_calls` da API.
#
# E o loop de tools do deepagents/langchain só anda com `tool_calls`
# ESTRUTURADO no `AIMessage`: conteúdo em prosa encerra o turno. Ou seja, o
# adapter chamaria a tool e o agente não perceberia — pior que não chamar.
# Contornar exigiria (a) subir um segundo mlx_lm.server com `--chat-template`
# hermes, que é launch-time e não cabe num campo de registro, ou (b) um
# BaseChatModel nosso reparseando texto em tool_calls. Os dois são decisão de
# arquitetura, não detalhe de config — até lá o load falha e diz por quê.
#
# (Nota que fecha o caso comum: o template do Qwen3.5-4B, base de toda a frota
# atual, JÁ é hermes — `<tools>` no system, `<tool_call>` na saída, parser
# `json_tools` inferido. Para esses pesos "native" e "hermes" são a mesma coisa
# e o campo não tem o que fazer.)
VALID_TOOL_FORMATS = frozenset({"native", "hermes"})
SUPPORTED_TOOL_FORMATS = frozenset({"native"})

# A frota só existe em runtime local: o adapter é um arquivo de peso na máquina.
# Escalação que troca o executor por nuvem não tem onde aplicá-lo, então o id
# morre ali em vez de viajar como enfeite no trace.
LOCAL_BACKEND = "deepagents"
LOCAL_MODEL_PREFIX = "openai:"


class AdapterError(Exception):
    """adapters.toml inválido, ou unidade pedindo adapter que não existe."""


@dataclass(frozen=True)
class Adapter:
    """Uma entrada do registro. `ref` é o diretório do adapter no disco — é ele
    que vai no corpo do request; `served_model` é o nome do modelo BASE como o
    runtime o conhece (o adapter só existe colado nele)."""

    id: str
    runtime: str
    served_model: str
    ref: str
    kinds: tuple[str, ...] = ()
    # Tokens que descrevem o que este peso sabe fazer — a matéria-prima do
    # ranking. Vazio = só entra por `paths` ou por pedido explícito da unidade.
    match: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    # Prefixo do system prompt do agente: o contexto em que este peso foi
    # treinado, aplicado em `deepagents_backend._build_agent`.
    system: str = ""
    tool_format: str = "native"
    enable_thinking: bool = False
    temperature: float | None = None
    top_p: float | None = None
    repeat_penalty: float | None = None
    max_tokens: int | None = None
    output: str = "text"
    scale: float | None = None
    # Desempate: adapter mais específico ganha do genérico com o mesmo score.
    priority: int = 0


def adapters_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else config_dir() / ADAPTERS_FILE


def load_adapters(path: Path | str | None = None) -> list[Adapter]:
    """Registro validado, na ordem do arquivo. Sem arquivo => frota vazia."""
    p = adapters_path(path)
    if not p.is_file():
        return []
    try:
        cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise AdapterError(f"adapters.toml ilegível: {p} ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise AdapterError(f"adapters.toml inválido: {p} ({e})") from e

    out: list[Adapter] = []
    vistos: set[str] = set()
    for raw in cfg.get("adapter") or []:
        out.append(_parse(raw, vistos))
    return out


def _parse(raw: dict, vistos: set[str]) -> Adapter:
    aid = str(raw.get("id", "")).strip()
    if not aid:
        raise AdapterError("[[adapter]] sem id")
    if aid in vistos:
        raise AdapterError(f"adapter {aid!r} duplicado")
    vistos.add(aid)

    runtime = str(raw.get("runtime", ""))
    if runtime not in VALID_RUNTIMES:
        raise AdapterError(
            f"adapter {aid!r}: runtime {runtime!r} não existe: {sorted(VALID_RUNTIMES)}"
        )

    served = str(raw.get("served_model", "")).strip()
    if not served:
        raise AdapterError(f"adapter {aid!r}: served_model vazio")

    ref = str(raw.get("ref", "")).strip()
    if not ref:
        raise AdapterError(f"adapter {aid!r}: ref vazio")
    ref_path = Path(ref).expanduser()
    if not ref_path.is_dir():
        raise AdapterError(f"adapter {aid!r}: ref não é um diretório: {ref}")

    kinds = tuple(str(k) for k in raw.get("kinds", []))
    fora = [k for k in kinds if k not in VALID_KINDS]
    if fora:
        raise AdapterError(f"adapter {aid!r}: kinds {fora} não são Kind: {sorted(VALID_KINDS)}")

    output = str(raw.get("output", "text"))
    if output not in VALID_OUTPUTS:
        raise AdapterError(
            f"adapter {aid!r}: output {output!r} não existe: {sorted(VALID_OUTPUTS)}"
        )

    tool_format = str(raw.get("tool_format", "native"))
    if tool_format not in VALID_TOOL_FORMATS:
        raise AdapterError(
            f"adapter {aid!r}: tool_format {tool_format!r} não existe: "
            f"{sorted(VALID_TOOL_FORMATS)}"
        )
    # Vocabulário válido que o backend ainda não sabe servir para no load, não
    # em silêncio: peso que fala hermes num base não-hermes chamaria a tool sem
    # ninguém do outro lado escutando (ver VALID_TOOL_FORMATS).
    if tool_format not in SUPPORTED_TOOL_FORMATS:
        raise AdapterError(
            f"adapter {aid!r}: tool_format {tool_format!r} aguardando suporte no backend"
        )

    return Adapter(
        id=aid,
        runtime=runtime,
        served_model=served,
        ref=str(ref_path),
        kinds=kinds,
        match=tuple(str(m) for m in raw.get("match", [])),
        paths=tuple(str(g) for g in raw.get("paths", [])),
        system=str(raw.get("system", "")),
        tool_format=tool_format,
        enable_thinking=bool(raw.get("enable_thinking", False)),
        temperature=_num(raw, "temperature", float, aid),
        top_p=_num(raw, "top_p", float, aid),
        repeat_penalty=_num(raw, "repeat_penalty", float, aid),
        max_tokens=_num(raw, "max_tokens", int, aid),
        output=output,
        scale=_num(raw, "scale", float, aid),
        priority=_num(raw, "priority", int, aid) or 0,
    )


def _num(raw: dict, key: str, cast, aid: str):
    """Ausente = None = "não opinar" (o backend usa o default dele)."""
    valor = raw.get(key)
    if valor is None:
        return None
    try:
        return cast(valor)
    except (TypeError, ValueError) as e:
        raise AdapterError(f"adapter {aid!r}: {key} inválido ({valor!r})") from e


def get_adapter(adapter_id: str | None, path: Path | str | None = None) -> Adapter | None:
    """Resolve um id do registro. Id desconhecido => None (base pura)."""
    if not adapter_id:
        return None
    for a in load_adapters(path):
        if a.id == adapter_id:
            return a
    return None


def runs_local(backend: str, model: str | None) -> bool:
    """Esta seleção roda no runtime da máquina? Só aí um adapter faz sentido."""
    return backend == LOCAL_BACKEND and (model or "").startswith(LOCAL_MODEL_PREFIX)


def select_adapter(
    unit: UnitSpec,
    kind: str | None = None,
    files: list[str] | None = None,
    path: Path | str | None = None,
) -> Adapter | None:
    """Adapter desta unidade, ou None (modelo base).

    `kinds` vazio = vale para todo kind, contrato idêntico ao de `Skill.kinds`.
    Depois do filtro por kind vêm os dois eixos das skills, na mesma ordem:
    path-trigger determinístico (`paths`, globs) e ranking por token entre o
    prompt da unidade e `match`. Empate no score: maior `priority`, depois a
    ordem do arquivo.

    Diferença deliberada em relação a `select_skills`: adapter com score zero
    NUNCA entra. Skill irrelevante custa contexto; adapter errado troca o peso
    do modelo — o custo de um falso positivo não é o mesmo, então o default é a
    base pura.

    `files` são os alvos conhecidos da unidade; None = os paths citados no
    prompt, a mesma fonte barata que o backend usa para as skills.
    """
    fleet = load_adapters(path)
    if not fleet:
        return None

    if unit.adapter:
        for a in fleet:
            if a.id == unit.adapter:
                return a
        raise AdapterError(f"unit {unit.id}: adapter {unit.adapter!r} não está no registro")

    matched = [a for a in fleet if not a.kinds or kind in a.kinds]
    if not matched:
        return None
    alvos = _unit_files(unit) if files is None else files
    triggered = [a for a in matched if _path_hit(a, alvos)]
    if triggered:
        return _top(triggered, fleet)

    wanted = _tokens(unit.prompt or "")
    if not wanted:
        return None
    scored = [(len(wanted & _tokens(" ".join(a.match))), a) for a in matched]
    best = max(score for score, _ in scored)
    if best <= 0:
        return None
    return _top([a for score, a in scored if score == best], fleet)


def _top(candidatos: list[Adapter], fleet: list[Adapter]) -> Adapter:
    return min(candidatos, key=lambda a: (-a.priority, fleet.index(a)))


def _unit_files(unit: UnitSpec) -> list[str]:
    """Paths citados no prompt. Mesma heurística do classificador de kind —
    token com extensão — para os dois eixos não discordarem sobre o que é
    arquivo."""
    from harness.routing.kinds import _FILE_RE

    return _FILE_RE.findall(unit.prompt or "")
