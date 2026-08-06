"""O gate de auto-aprovação: quando o loop pode ligar a própria proposta sem
humano no meio.

Vive no zoneamento imutável do genoma pelo mesmo motivo do resto de `ruler/`:
um gate que abaixa o próprio limiar se aprova sozinho. Três garantias que este
módulo existe para não perder:

1. `runner=real` sozinho NÃO é medição de comportamento. `tune._run_case_real`
   cai no extrativo em silêncio (LM Studio fora do ar, timeout, teto de custo
   estourado) e o carimbo da cadeia continuava dizendo `runner=real probe=ok`
   com metade dos casos julgados por substring. A fração REAL de casos que
   passaram pelo modelo (`real_trial_frac`) é o que fecha esse buraco.
2. Um exame que pode ser recongelado não é um exame. `[selfapprove.pinned]`
   amarra CADA artefato a um par `versão do manifest + sha256 do bundle` — só
   o humano escreve esse par, e ele é o único jeito de o gate aceitar medir.
3. Config que CONCEDE permissão só vale vinda da árvore protegida:
   `~/.harness/config` e um `config/` de qualquer cwd não concedem nada — só
   `$HARNESS_ROOT/config/selfapprove.toml`, com o genoma daquela mesma raiz
   listando o arquivo por nome em `immutable`, carrega thresholds diferentes do
   default (tudo desligado).

Import: stdlib + `harness.paths`, nunca `harness.improve.*` nem
`harness.genome.*` — a checagem de pertencimento ao genoma é uma leitura crua
de TOML, de propósito, para este módulo continuar sem dependência do resto do
pacote de melhoria (o mesmo motivo que mantém `ruler/gate.py` isolado).
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from harness import paths

ACTIVATE, HUMAN_QUEUE, NO_CHANGE = "activate", "human_queue", "no_change"
INTERNAL, EXTERNAL = "internal", "external"
ENV_FLAG = "HARNESS_SELFAPPROVE"  # "0" desliga; default ligado
ROOT_ENV = "HARNESS_ROOT"  # duplicado de improve/__init__: ruler não importa improve
CONFIG_NAME = "selfapprove.toml"
CONFIG_REL = "config/selfapprove.toml"
GENOME_REL = "config/genome.toml"
SECTION = "selfapprove"
RUNNER_REAL, PROBE_OK = "real", "ok"  # duplicados de tune pelo mesmo motivo

_PIN_RE = re.compile(r"^v\d+:[0-9a-f]{64}$")


@dataclass(frozen=True)
class Thresholds:
    """Os limiares vigentes. O `Thresholds()` puro (sem argumento nenhum) é o
    estado "tudo desligado" — o que `load_thresholds` devolve toda vez que a
    leitura não pode provar que veio da árvore protegida."""

    enabled: bool = False
    external_enabled: bool = False
    require_measured_behavior: bool = True
    min_delta: float = 0.05
    min_delta_vs_none: float = 0.0
    min_real_trial_frac: float = 1.0
    min_cases: int = 4
    min_holdout_cases: int = 1
    min_trials: int = 8
    min_holdout_trials: int = 2
    holdout_tolerance: float = 0.0
    pinned: tuple[tuple[str, str], ...] = ()

    def pin_for(self, artifact: str) -> str:
        return next((v for a, v in self.pinned if a == artifact), "")


@dataclass(frozen=True)
class Evidence:
    """O que foi medido, só primitivos: atravessa checkpoint como dict, e é o
    que `decide` julga. `TuneOutcome` é entrada não confiável — quem monta isto
    (`improve/selfapprove.build_evidence`) já filtrou tudo que podia mentir."""

    artifact: str
    target_file: str
    origin: str = INTERNAL
    winner: int = 1
    winner_valid: bool = True
    runner: str = ""
    probe: str = ""
    real_ok: int = 0
    real_fallback: int = 0
    draft_is_incumbent: bool = False
    eval_violations: tuple[str, ...] = ()
    bundle_version: int = 0
    bundle_sha256: str = ""
    measure_error: str = ""
    security_ran: bool = False
    security_findings: tuple[str, ...] = ()
    before_overall: float = 0.0
    after_overall: float = 0.0
    none_overall: float = 0.0
    before_holdout: float = 0.0
    after_holdout: float = 0.0
    n_total: int = 0
    n_holdout: int = 0
    n_cases: int = 0
    n_holdout_cases: int = 0
    ruler_version: int = 0
    cost_usd: float = 0.0
    backend_calls: int = 0
    cost_unknown: int = 0

    @property
    def real_trial_frac(self) -> float:
        tot = self.real_ok + self.real_fallback
        return self.real_ok / tot if tot else 0.0

    @property
    def bundle_pin(self) -> str:
        return f"v{self.bundle_version}:{self.bundle_sha256}"


@dataclass(frozen=True)
class Verdict:
    """O veredito de `decide`. `threshold` é sempre `th.min_delta` — é o único
    número que todo relatório quer ao lado do delta, mesmo quando a decisão saiu
    por outro motivo antes de chegar ao gate de delta."""

    decision: str
    reason: str
    delta: float
    threshold: float
    stamp: str


def _bool(raw: object, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _num(raw: object, default: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _int(raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    if raw < 0:
        return default
    return raw


def _pinned(raw: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    out: list[tuple[str, str]] = []
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and _PIN_RE.match(v):
            out.append((k, v))
    return tuple(out)


def load_thresholds(path: Path | None = None) -> Thresholds:
    """Os limiares vigentes, FAIL-CLOSED em cada ponto.

    Config que CONCEDE permissão só vale vinda da árvore protegida:
    `~/.harness/config` e um `config/` de cwd qualquer não concedem nada. Só
    conta quando o arquivo lido é, por `realpath`, o mesmo
    `$HARNESS_ROOT/config/selfapprove.toml`, e o `genome.toml` daquela MESMA
    raiz lista `config/selfapprove.toml` em `immutable` — por nome exato, não
    por glob: o ponto é o genoma nomear ESTE arquivo, não um padrão que por
    acaso o cobre. Qualquer falha nessa cadeia (ausente, ilegível, torto, fora
    da árvore, não listado) devolve `Thresholds()` — tudo desligado, e toda
    proposta vai para a fila humana.
    """
    if os.environ.get(ENV_FLAG, "1").strip() == "0":
        return Thresholds()

    p = Path(path) if path is not None else paths.config_file(CONFIG_NAME)
    root = Path(os.environ.get(ROOT_ENV, "."))
    expected = root / CONFIG_REL
    try:
        if not p.is_file() or not expected.is_file():
            return Thresholds()
        if os.path.realpath(p) != os.path.realpath(expected):
            return Thresholds()
        gdata = tomllib.loads((root / GENOME_REL).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError):
        return Thresholds()
    immutable = gdata.get("immutable")
    if not isinstance(immutable, list) or CONFIG_REL not in immutable:
        return Thresholds()

    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Thresholds()
    sec = data.get(SECTION)
    if not isinstance(sec, dict):
        return Thresholds()

    d = Thresholds()
    ext = sec.get("external")
    ext_enabled = (
        _bool(ext.get("enabled"), d.external_enabled) if isinstance(ext, dict) else d.external_enabled
    )
    # Clamp, não fallback: fora de faixa vira o teto, não o default congelado —
    # é o único campo em que "exagerou" tem resposta melhor que "ignora tudo".
    frac = min(1.0, _num(sec.get("min_real_trial_frac"), d.min_real_trial_frac))

    return Thresholds(
        enabled=_bool(sec.get("enabled"), d.enabled),
        external_enabled=ext_enabled,
        require_measured_behavior=_bool(
            sec.get("require_measured_behavior"), d.require_measured_behavior
        ),
        min_delta=_num(sec.get("min_delta"), d.min_delta),
        min_delta_vs_none=_num(sec.get("min_delta_vs_none"), d.min_delta_vs_none),
        min_real_trial_frac=frac,
        min_cases=_int(sec.get("min_cases"), d.min_cases),
        min_holdout_cases=_int(sec.get("min_holdout_cases"), d.min_holdout_cases),
        min_trials=_int(sec.get("min_trials"), d.min_trials),
        min_holdout_trials=_int(sec.get("min_holdout_trials"), d.min_holdout_trials),
        holdout_tolerance=_num(sec.get("holdout_tolerance"), d.holdout_tolerance),
        pinned=_pinned(sec.get("pinned")),
    )


def decide(ev: Evidence, th: Thresholds) -> Verdict:
    """O veredito, em ordem — primeiro critério que bate decide. NUNCA devolve
    rejeição: o pior caso é `human_queue`, porque nada aqui é irreversível — só
    escrita automática é. `reason` sempre vem prefixado `selfapprove:`."""
    delta = ev.after_overall - ev.before_overall
    decision, reason = _decide(ev, th, delta)
    full_reason = f"selfapprove:{reason}"
    s = stamp(ev, decision, full_reason, th)
    return Verdict(
        decision=decision, reason=full_reason, delta=delta, threshold=th.min_delta, stamp=s
    )


def _decide(ev: Evidence, th: Thresholds, delta: float) -> tuple[str, str]:
    if ev.winner <= 1:
        return NO_CHANGE, "no-candidate"
    if not ev.winner_valid:
        return HUMAN_QUEUE, "invalid-winner"
    if not th.enabled:
        return HUMAN_QUEUE, "off"
    if ev.origin == EXTERNAL and not th.external_enabled:
        return HUMAN_QUEUE, "external-optin-off"
    if ev.security_findings:
        return HUMAN_QUEUE, "security:" + ",".join(ev.security_findings)
    if not ev.security_ran:
        return HUMAN_QUEUE, "security-not-run"
    if ev.eval_violations:
        return HUMAN_QUEUE, "eval-tampered:" + ",".join(ev.eval_violations)
    if ev.measure_error:
        return HUMAN_QUEUE, f"measure-error:{ev.measure_error}"
    if not th.pin_for(ev.artifact):
        return HUMAN_QUEUE, "bundle-unpinned"
    if th.pin_for(ev.artifact) != ev.bundle_pin:
        return HUMAN_QUEUE, f"bundle-changed:{ev.bundle_pin[:20]}"
    if not ev.draft_is_incumbent:
        return HUMAN_QUEUE, "draft-not-incumbent"
    if th.require_measured_behavior and (ev.runner != RUNNER_REAL or ev.probe != PROBE_OK):
        return HUMAN_QUEUE, "behavior-not-measured"
    if th.require_measured_behavior and ev.real_trial_frac < th.min_real_trial_frac:
        return (
            HUMAN_QUEUE,
            f"behavior-partial:{ev.real_trial_frac:.2f}<{th.min_real_trial_frac:.2f}",
        )
    if ev.n_cases < th.min_cases:
        return HUMAN_QUEUE, f"cases-too-few:n={ev.n_cases}<{th.min_cases}"
    if ev.n_holdout_cases < th.min_holdout_cases:
        return HUMAN_QUEUE, f"holdout-cases-too-few:n={ev.n_holdout_cases}<{th.min_holdout_cases}"
    if ev.n_total < th.min_trials:
        return HUMAN_QUEUE, f"sample-too-small:n={ev.n_total}<{th.min_trials}"
    if ev.n_holdout < th.min_holdout_trials:
        return HUMAN_QUEUE, f"holdout-too-small:n={ev.n_holdout}<{th.min_holdout_trials}"
    if ev.after_holdout < ev.before_holdout - th.holdout_tolerance:
        return HUMAN_QUEUE, f"holdout-regressed:{ev.before_holdout:.3f}->{ev.after_holdout:.3f}"
    if ev.after_overall <= ev.none_overall + th.min_delta_vs_none:
        return HUMAN_QUEUE, f"no-lift-vs-none:{ev.after_overall:.3f}<={ev.none_overall:.3f}"
    if delta < th.min_delta:
        return HUMAN_QUEUE, f"below-threshold:delta={delta:+.3f}<{th.min_delta:.3f}"
    return ACTIVATE, f"ok:delta={delta:+.3f}>={th.min_delta:.3f}"


def stamp(ev: Evidence, decision: str, reason: str, th: Thresholds) -> str:
    """Uma linha determinística: o que foi medido, contra o que, e o veredito.
    É o que vai para o `note` do ledger e para o stdout do CLI."""
    delta = ev.after_overall - ev.before_overall
    if ev.security_ran and not ev.security_findings:
        sec = "ok"
    elif not ev.security_ran:
        sec = "nao-rodou"
    else:
        sec = ",".join(ev.security_findings)
    s = (
        f"before={ev.before_overall:.3f} after={ev.after_overall:.3f} delta={delta:+.3f} "
        f"min={th.min_delta:.3f} none={ev.none_overall:.3f} "
        f"hold={ev.before_holdout:.3f}->{ev.after_holdout:.3f} "
        f"n={ev.n_total}/{ev.n_holdout} cases={ev.n_cases}/{ev.n_holdout_cases} "
        f"real={ev.real_ok}/{ev.real_fallback} runner={ev.runner or '-'} probe={ev.probe or '-'} "
        f"ruler=v{ev.ruler_version} bundle={ev.bundle_pin[:20] if ev.bundle_sha256 else '-'} "
        f"sec={sec} cost=${ev.cost_usd:.4f}/{ev.backend_calls} origin={ev.origin} -> {decision}"
    )
    if decision != ACTIVATE:
        s += f" ({reason})"
    return s
