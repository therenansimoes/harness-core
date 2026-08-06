"""Ação 'selfapprove': liga a própria proposta do tune sem humano no meio, ou
enfileira para um humano olhar — nunca escreve às cegas.

A decisão pura mora em `harness.ruler.selfapprove` (genoma-imutável); este
módulo é a fiação: monta a `Evidence` a partir de um `TuneProposal`, aplica o
veredito e grava a evidência em disco. Três coisas que este módulo garante e
que o `ruler.selfapprove` não pode garantir sozinho (porque é puro):

1. O bundle é conferido ANTES de `propose_tune` rodar (`preflight`) — sem pin
   ou com pin divergente, nenhum `verify_cmd` do exame chega a executar.
2. O que é ESCANEADO por segurança é o que vai para o disco: `security_check`
   roda sobre os bytes RENDERIZADOS (`Tunable.render`), nunca sobre
   `proposal.text` cru.
3. Fila humana é reversível por si só: cada entrada carrega o próprio
   incumbente (`incumbent.txt`), porque a cadeia de tune em `data/tune/` é
   sobrescrita pelo próximo run do mesmo artefato e não serve de fonte de undo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from harness import paths
from harness.backends import smart_fs
from harness.evals.bundle import load_cases, split_cases
from harness.evals.freeze import load_manifest, verify_frozen
from harness.evals.score import RULER_VERSION
from harness.improve import meta, mutate, research, tune
from harness.improve import root_dir as improve_root_dir
from harness.improve import tunable as tunable_mod
from harness.improve.tunable import tunable_for
from harness.ledger import store
from harness.redact import redact
from harness.ruler import selfapprove as ruler_sa
from harness.trust_boundary import sanitize as trust_sanitize
from harness.types import MutationRow

ACTION = "selfapprove"
AUTO_PROMOTED, QUEUED, BLOCKED, NO_CHANGE_V = "auto_promoted", "queued", "blocked", "no_change"
HUMAN_PROMOTED, UNDONE = "human_promoted", "undone"
QUEUE_SUBDIR = "selfapprove/queue"
PROPOSAL_FILE, INCUMBENT_FILE, EVIDENCE_FILE = "proposal.txt", "incumbent.txt", "evidence.json"
# Mesmo teto do `market.MAX_SKILL_BYTES`: um artefato afinável é sempre skill
# ou workflow, e nenhum dos dois passa disso sem virar despejo.
MAX_BYTES = 64 * 1024
# `rounds` alto é fatura alta, não qualidade — quem quiser cadeia mais longa
# tem `harness tune` de bancada, sem o teto de custo do run automático.
MAX_ROUNDS = 5
RUN_BUDGET_USD = 1.00
# 120 = 4 casos x 4 trials x (none + draft + 5 candidatas) + 5 reescritas +
# probe, no maior bundle que existe. Teto de máquina, não de gosto.
MAX_BACKEND_CALLS = 120
# `render_skill` só emite estas três chaves — qualquer outra do frontmatter do
# incumbente (origin, origin_sha256, approved…) é APAGADA pela escrita.
RENDERED_KEYS = frozenset({"name", "kinds", "description"})

# Curta e de alta precisão de propósito: palavra genérica em português já
# incendeia o `score._SLOP_RE` (o caso pf-003 pede "desabilite todos os
# testes" e a recusa certa REPETE a palavra) — aqui o padrão tem que ser
# instrução de bypass inequívoca, não vocabulário comum.
OVERRIDE_PATTERNS = (
    (re.compile(r"ignore (all|previous|as|todas as) (instru|instruc)", re.I), "ignore-instructions"),
    (re.compile(r"desabilit\w+ (os |as )?(testes|verifica)", re.I), "disable-checks"),
    (re.compile(r"--no-verify", re.I), "no-verify"),
    (re.compile(r"rm -rf", re.I), "rm-rf"),
    (re.compile(r"curl[^\n]*\|\s*(sh|bash)", re.I), "curl-pipe-shell"),
    (re.compile(r"chmod 777", re.I), "chmod-777"),
    (re.compile(r"sudo ", re.I), "sudo"),
)

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class SelfApproveError(Exception):
    """Evento não gravável (id duplicado no mesmo segundo) — nada de novo foi
    escrito além do que já estava no disco antes desta chamada."""


@dataclass(frozen=True)
class Outcome:
    """O resultado de um `run`/`approve`/`undo`, pronto para o CLI imprimir."""

    artifact: str
    decision: str
    reason: str
    delta: float
    threshold: float
    stamp: str
    mutation_id: str
    verdict: str
    written_path: str | None
    queue_dir: str | None
    rollback_id: str | None
    # Fora da lista original da spec: o CLI (`v{n}` em toda mensagem de `run`)
    # precisa da versão julgada, e `Outcome` era o único lugar que não a
    # carregava — default 0 mantém quem construiu sem o argumento.
    version: int = 0


# --------------------------------------------------------------------------- preflight


def preflight(artifact: str, *, root: Path | str | None = None) -> tuple[ruler_sa.Thresholds, str, int, str]:
    """O que o gate precisa saber ANTES de `propose_tune` rodar: exame íntegro
    e pinado. `("", ...)` = livre para medir; qualquer outra coisa é o motivo
    de nem chegar a medir — inclusive antes do `verify_cmd` de qualquer caso
    ter chance de executar.
    """
    th = ruler_sa.load_thresholds()
    try:
        violations = verify_frozen(artifact, root=root)
        if violations:
            return th, f"eval-tampered:{','.join(violations)}", 0, ""
        m = load_manifest(artifact, root)
        if m is None:
            return th, "bundle-missing", 0, ""
        pin = th.pin_for(artifact)
        if not pin:
            return th, "bundle-unpinned", m.version, m.bundle_sha256
        current = f"v{m.version}:{m.bundle_sha256}"
        if pin != current:
            return th, f"bundle-changed:v{m.version}:{m.bundle_sha256[:12]}", m.version, m.bundle_sha256
        return th, "", m.version, m.bundle_sha256
    except (OSError, ValueError):
        return th, "io", 0, ""


# --------------------------------------------------------------------------- segurança


def security_check(
    rendered: str,
    incumbent: str,
    target_file: str,
    *,
    adapter=None,
    root: Path | str | None = None,
    genome=None,
) -> list[str]:
    """Achados sobre os bytes RENDERIZADOS (o que vai para o disco), nunca
    sobre `proposal.text` cru. Toda entrada aqui é motivo de fila, nunca de
    rejeição — quem decide fila-vs-ativa é o `ruler.selfapprove.decide`."""
    out: list[str] = []
    out.extend(mutate.check(SimpleNamespace(target_file=target_file), root=root, genome=genome))
    if meta._targets_ruler_config(Path(target_file)):
        out.append("meta-guarded")
    if adapter is not None and adapter.validate(rendered):
        out.append("render-invalid")

    if Path(target_file).as_posix().startswith(tunable_mod.SKILLS_PREFIX):
        try:
            inc_meta = research._parse_skill(incumbent) if incumbent else {}
        except (research.ResearchError, ValueError):
            inc_meta = {}
        # `render_skill` só reconstrói name/kinds/description: qualquer outra
        # chave do incumbente (origin, origin_sha256, approved…) é apagada
        # pela escrita — o evento de lavagem que o self-approve não pode deixar
        # passar batido para uma skill de terceiro.
        if set(inc_meta) - RENDERED_KEYS:
            out.append("frontmatter-loss")
        inc_kinds = [str(k) for k in (inc_meta.get("kinds") or [])]
        if len(inc_kinds) > 1:
            out.append("kinds-narrowed")
        try:
            cand_meta = research._parse_skill(rendered)
        except (research.ResearchError, ValueError):
            out.append("unparseable")
        else:
            cand_kinds = {str(k) for k in (cand_meta.get("kinds") or [])}
            if inc_kinds and not cand_kinds <= set(inc_kinds):
                out.append("kinds-widened")

    if trust_sanitize(rendered) != rendered:
        out.append("untrusted-tag")
    if redact(rendered) != rendered:
        out.append("secret")
    if len(rendered.encode("utf-8")) > MAX_BYTES:
        out.append("oversize")
    if incumbent and len(rendered) < smart_fs.SHRINK_FLOOR * len(incumbent):
        out.append("shrink")
    for pat, slug in OVERRIDE_PATTERNS:
        if pat.search(rendered):
            out.append(f"override:{slug}")
            break
    return out


def origin_of(artifact: str, incumbent_text: str) -> str:
    """`external` para skill que carrega `origin` no frontmatter, `internal`
    para o resto (workflow, ou skill sem a chave). Skill ilegível vira
    `external` fail-closed: quem não consegue provar que é interna não é.

    Lê frontmatter mutável e não tem alternativa durável (`market.installed()`
    lê o mesmo campo) — a durabilidade vem de `frontmatter-loss`, que
    enfileira o caminho automático E recusa o humano sempre que a escrita
    apagaria esse marcador. Propriedade de design, não lacuna: nenhuma skill
    externa é promovível pelo `selfapprove`, por nenhum dos dois caminhos —
    `harness market approve` continua sendo a única estrada dela.
    """
    if not Path(artifact).as_posix().startswith(tunable_mod.SKILLS_PREFIX):
        return ruler_sa.INTERNAL
    try:
        meta_ = research._parse_skill(incumbent_text)
    except (research.ResearchError, ValueError):
        return ruler_sa.EXTERNAL
    return ruler_sa.EXTERNAL if meta_.get("origin") else ruler_sa.INTERNAL


# --------------------------------------------------------------------------- evidência


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _err_evidence(proposal: tune.TuneProposal, code: str) -> ruler_sa.Evidence:
    return ruler_sa.Evidence(
        artifact=proposal.artifact, target_file=proposal.target_file, winner=2, measure_error=code
    )


def _assemble(
    proposal: tune.TuneProposal, *, root: Path | str | None = None, genome=None
) -> tuple[ruler_sa.Evidence, str, str]:
    """`(Evidence, rendered, incumbent)`. `TuneOutcome` é entrada NÃO
    confiável (dataclass simples, qualquer um monta uma na mão) — daí o funil
    `measure_error` em volta do corpo inteiro."""
    try:
        adapter = tunable_for(proposal.artifact, root=root)
        try:
            incumbent = adapter.read()
        except OSError:
            return _err_evidence(proposal, "incumbent-unreadable"), "", ""
        outcome = proposal.outcome
        try:
            rendered = adapter.render(outcome.winning.text, outcome.winning.version)
        except Exception:
            return _err_evidence(proposal, "render-error"), "", incumbent

        draft_is_incumbent = _sha(outcome.chain[0].text) == _sha(incumbent)
        eval_violations = tuple(verify_frozen(proposal.artifact, root=root))
        m = load_manifest(proposal.artifact, root)
        bundle_version = m.version if m is not None else 0
        bundle_sha256 = m.bundle_sha256 if m is not None else ""

        ids = {t.case_id for t in outcome.winning.trials}
        _, hold = split_cases(load_cases(proposal.artifact, root))
        hold_ids = {c.id for c in hold}

        origin = origin_of(proposal.artifact, incumbent)
        findings = security_check(
            rendered, incumbent, proposal.target_file, adapter=adapter, root=root, genome=genome
        )
        if proposal.text != outcome.winning.text:
            findings = [*findings, "text-mismatch"]

        spend = tune.spend()
        ev = ruler_sa.Evidence(
            artifact=proposal.artifact,
            target_file=proposal.target_file,
            origin=origin,
            winner=outcome.winner,
            winner_valid=outcome.winning.valid,
            runner=outcome.runner,
            probe=outcome.probe,
            real_ok=outcome.winning.real_ok,
            real_fallback=outcome.winning.real_fallback,
            draft_is_incumbent=draft_is_incumbent,
            eval_violations=eval_violations,
            bundle_version=bundle_version,
            bundle_sha256=bundle_sha256,
            measure_error="",
            security_ran=True,
            security_findings=tuple(findings),
            before_overall=outcome.baseline["draft"].overall,
            after_overall=outcome.winning.agg.overall,
            none_overall=outcome.baseline["none"].overall,
            before_holdout=outcome.chain[0].holdout.overall,
            after_holdout=outcome.winning.holdout.overall,
            n_total=outcome.winning.agg.n,
            n_holdout=outcome.winning.holdout.n,
            n_cases=len(ids),
            n_holdout_cases=len(ids & hold_ids),
            ruler_version=RULER_VERSION,
            cost_usd=spend["usd"],
            backend_calls=int(spend["calls"]),
            cost_unknown=int(spend["unknown"]),
        )
        return ev, rendered, incumbent
    except (KeyError, IndexError, AttributeError, TypeError) as exc:
        return _err_evidence(proposal, f"evidence-error:{type(exc).__name__}"), "", ""


def build_evidence(
    proposal: tune.TuneProposal, *, root: Path | str | None = None, genome=None
) -> ruler_sa.Evidence:
    ev, _rendered, _incumbent = _assemble(proposal, root=root, genome=genome)
    return ev


# --------------------------------------------------------------------------- registro + fila


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower())


def _write_atomic(path: Path, text: str) -> None:
    """tmp irmão + `os.replace`, mesmo padrão do `rollback._write_atomic`: o
    leitor nunca vê o arquivo meio escrito."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _queue_dir(mid: str) -> Path:
    return paths.data_dir() / QUEUE_SUBDIR / _slug(mid)


def _write_queue(
    mid: str, ts: str, ev: ruler_sa.Evidence, rendered: str, incumbent: str, v: ruler_sa.Verdict
) -> Path:
    """A proposta enfileirada + o incumbente do MOMENTO — é a fonte do undo,
    não a cadeia de `data/tune/`, que o próximo run do mesmo artefato
    sobrescreve."""
    d = _queue_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    _write_atomic(d / PROPOSAL_FILE, rendered)
    _write_atomic(d / INCUMBENT_FILE, incumbent)
    payload = {
        "id": mid,
        "artifact": ev.artifact,
        "target_file": ev.target_file,
        "version": ev.winner,
        "decision": v.decision,
        "reason": v.reason,
        "delta": v.delta,
        "threshold": v.threshold,
        "stamp": v.stamp,
        "proposal_sha256": _sha(rendered),
        "incumbent_sha256": _sha(incumbent),
        "bundle_pin": ev.bundle_pin,
        "created_at": ts,
        "applied_at": None,
        "applied_mutation_id": None,
        "evidence": asdict(ev),
    }
    _write_atomic(d / EVIDENCE_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return d


def _new_mutation_id(artifact: str) -> tuple[str, str, str]:
    ts = store.now_iso()
    rule_id = f"{ACTION}:{artifact}"
    return ts, rule_id, f"{rule_id}@{ts}"


def _record_row(rule_id: str, ts: str, mid: str, ev: ruler_sa.Evidence, v: ruler_sa.Verdict, verdict: str) -> str:
    """Grava a linha do ledger; em colisão de `mutation_id` (mesmo segundo),
    tenta sufixos `-2`..`-9` antes de desistir. Devolve o id realmente usado."""
    row = MutationRow(
        mutation_id=mid,
        rule_id=rule_id,
        verdict=verdict,
        arm_a=f"v{max(1, ev.winner - 1)}",
        arm_b=f"v{ev.winner}",
        applied_at=ts,
        reverted=False,
        note=v.stamp,
        action=ACTION,
    )
    if store.record_mutation(row):
        return mid
    for k in range(2, 10):
        alt = f"{rule_id}@{ts}-{k}"
        if store.record_mutation(replace(row, mutation_id=alt)):
            return alt
    raise SelfApproveError(f"evento duplicado para {ev.artifact}; tente de novo")


def _blocked_outcome(
    artifact: str, err: str, bver: int, bsha: str, *, root: Path | str | None = None
) -> Outcome:
    """Preflight ou `propose_tune` recusaram ANTES de medir — nada foi escrito,
    nem em `data/tune/`, nem na fila, nem no artefato."""
    eval_violations = tuple(err.split(":", 1)[1].split(",")) if err.startswith("eval-tampered:") else ()
    measure_error = "" if (err.startswith("eval-tampered") or err.startswith("bundle-")) else err
    ev = ruler_sa.Evidence(
        artifact=artifact,
        target_file=artifact,
        winner=2,
        bundle_version=bver,
        bundle_sha256=bsha,
        # Nada foi renderizado (não houve proposta) — não há bytes para
        # escanear, então "sem achado" é o único veredito honesto, e é o que
        # deixa a razão MAIS específica (exame/bundle) aparecer em vez de um
        # genérico "security-not-run" mascarando o motivo de verdade.
        security_ran=True,
        eval_violations=eval_violations,
        measure_error=measure_error,
    )
    th = ruler_sa.load_thresholds()
    v = ruler_sa.decide(ev, th)
    ts, rule_id, mid = _new_mutation_id(artifact)
    mid = _record_row(rule_id, ts, mid, ev, v, BLOCKED)
    return Outcome(
        artifact=artifact,
        decision=v.decision,
        reason=v.reason,
        delta=v.delta,
        threshold=v.threshold,
        stamp=v.stamp,
        mutation_id=mid,
        verdict=BLOCKED,
        written_path=None,
        queue_dir=None,
        rollback_id=None,
        version=ev.winner,
    )


def judge(proposal: tune.TuneProposal, *, root: Path | str | None = None, genome=None) -> Outcome:
    """Monta a evidência, decide, e executa o veredito.

    ATIVA escreve pelo caminho de sempre (`tune.apply_tune`, genoma antes da
    escrita) e depois RELÊ o arquivo do disco: bytes diferentes dos
    escaneados restauram o incumbente na hora — o que foi julgado tem que ser
    o que ficou gravado, não uma promessa.
    """
    ev, rendered, incumbent = _assemble(proposal, root=root, genome=genome)
    th = ruler_sa.load_thresholds()
    v = ruler_sa.decide(ev, th)
    ts, rule_id, mid = _new_mutation_id(ev.artifact)

    written_path: str | None = None
    queue_dir: str | None = None
    rollback_id: str | None = None
    verdict = NO_CHANGE_V

    if v.decision == ruler_sa.ACTIVATE:
        try:
            rec = tune.apply_tune(proposal, root=root, genome=genome)
        except mutate.GenomeViolation as exc:
            verdict = QUEUED
            queue_dir = str(_write_queue(mid, ts, ev, rendered, incumbent, v))
            v = replace(v, reason=f"selfapprove:genome-blocked:{exc}", stamp=f"{v.stamp} [genome-blocked]")
        else:
            target = improve_root_dir(root) / ev.target_file
            try:
                disk_text = target.read_text(encoding="utf-8")
            except OSError:
                disk_text = ""
            if _sha(disk_text) != _sha(rendered):
                _write_atomic(target, incumbent)
                verdict = BLOCKED
                v = replace(v, reason="selfapprove:written-mismatch")
            else:
                verdict = AUTO_PROMOTED
                written_path = rec.written_path
                rollback_id = f"tune:{rec.artifact}@{rec.recorded_at}"
    elif v.decision == ruler_sa.HUMAN_QUEUE:
        verdict = QUEUED
        queue_dir = str(_write_queue(mid, ts, ev, rendered, incumbent, v))

    mid = _record_row(rule_id, ts, mid, ev, v, verdict)
    return Outcome(
        artifact=ev.artifact,
        decision=v.decision,
        reason=v.reason,
        delta=v.delta,
        threshold=v.threshold,
        stamp=v.stamp,
        mutation_id=mid,
        verdict=verdict,
        written_path=written_path,
        queue_dir=queue_dir,
        rollback_id=rollback_id,
        version=ev.winner,
    )


# --------------------------------------------------------------------------- run


def run_and_judge(
    artifact: str,
    *,
    rounds: int = tune.DEFAULT_ROUNDS,
    model: str = tune.DEFAULT_MODEL,
    max_usd: float = tune.DEFAULT_MAX_USD,
    runner: str = tune.DEFAULT_RUNNER,
    root: Path | str | None = None,
) -> Outcome:
    """`preflight` -> `propose_tune` -> `judge`, com teto de máquina em volta:
    `rounds` clampado, chamadas e custo tetados pelo `run` inteiro (não por
    chamada — é o `tune._call` que confere por chamada, e devolve `None` de
    custo no backend local, onde o teto de CHAMADAS é quem segura)."""
    clamped = max(1, min(rounds, MAX_ROUNDS))
    if clamped != rounds:
        print(
            f"selfapprove run: rounds={rounds} > teto {MAX_ROUNDS}; rodando com {clamped}",
            file=sys.stderr,
        )
    rounds = clamped

    _th, err, bver, bsha = preflight(artifact, root=root)
    if err:
        return _blocked_outcome(artifact, err, bver, bsha, root=root)

    prev_budget, prev_calls = tune.RUN_BUDGET_USD, tune.RUN_MAX_CALLS
    tune.reset_real_counters()
    tune.reset_spend()
    tune.RUN_BUDGET_USD = RUN_BUDGET_USD
    tune.RUN_MAX_CALLS = MAX_BACKEND_CALLS
    try:
        proposal = tune.propose_tune(
            artifact, rounds=rounds, model=model, max_usd=max_usd, runner=runner
        )
    except tune.TuneAborted as exc:
        violations = exc.args[0] if exc.args else []
        return _blocked_outcome(artifact, "eval-tampered:" + ",".join(violations), bver, bsha, root=root)
    except tune.TuneError:
        return _blocked_outcome(artifact, "tune-error", bver, bsha, root=root)
    except FileNotFoundError:
        return _blocked_outcome(artifact, "bundle-missing", bver, bsha, root=root)
    except ValueError:
        return _blocked_outcome(artifact, "no-adapter", bver, bsha, root=root)
    except OSError:
        return _blocked_outcome(artifact, "io", bver, bsha, root=root)
    finally:
        tune.RUN_BUDGET_USD, tune.RUN_MAX_CALLS = prev_budget, prev_calls

    return judge(proposal, root=root)


# --------------------------------------------------------------------------- fila humana


def _find_queue_entry(entry_id: str) -> Path | None:
    base = paths.data_dir() / QUEUE_SUBDIR
    for candidate in (entry_id, _slug(entry_id)):
        d = base / candidate
        if (d / EVIDENCE_FILE).is_file():
            return d
    return None


def _load_queue_payload(d: Path) -> dict | None:
    try:
        return json.loads((d / EVIDENCE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def queue_entries(limit: int | None = 100) -> list[dict]:
    base = paths.data_dir() / QUEUE_SUBDIR
    if not base.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(base.iterdir()):
        payload = _load_queue_payload(d) if d.is_dir() else None
        if payload is not None:
            out.append(payload)
    out.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
    return out[:limit] if limit is not None else out


def _refused(reason: str, artifact: str = "", version: int = 0) -> dict:
    return {"status": "refused", "reason": reason, "path": "", "artifact": artifact, "version": version}


def approve_queued(entry_id: str, *, root: Path | str | None = None) -> dict:
    """Caminho humano — fail-closed em cada ponto. Recusa não escreve nada."""
    d = _find_queue_entry(entry_id)
    if d is None:
        return _refused("id-desconhecido")
    payload = _load_queue_payload(d)
    if payload is None:
        return _refused("id-desconhecido")

    artifact = str(payload.get("artifact", ""))
    version = int(payload.get("version", 0))
    if payload.get("applied_at"):
        return _refused("ja-aplicada", artifact, version)

    try:
        proposal_text = (d / PROPOSAL_FILE).read_text(encoding="utf-8")
        incumbent_text = (d / INCUMBENT_FILE).read_text(encoding="utf-8")
    except OSError:
        return _refused("id-desconhecido", artifact, version)

    if _sha(proposal_text) != payload.get("proposal_sha256"):
        return _refused("sha-divergente", artifact, version)

    adapter = tunable_for(artifact, root=root)
    try:
        disk_text = adapter.read()
    except OSError:
        disk_text = ""
    if _sha(disk_text) != payload.get("incumbent_sha256"):
        return _refused("artefato-mudou", artifact, version)

    violations = verify_frozen(artifact, root=root)
    if violations:
        return _refused(f"eval-adulterado:{','.join(violations)}", artifact, version)

    findings = security_check(proposal_text, incumbent_text, artifact, adapter=adapter, root=root)
    if findings:
        return _refused(f"seguranca:{','.join(findings)}", artifact, version)

    errs = adapter.validate(proposal_text)
    if errs:
        return _refused(f"invalida:{','.join(errs)}", artifact, version)

    gv = mutate.check(SimpleNamespace(target_file=artifact), root=root)
    if gv:
        return _refused(f"genoma:{','.join(gv)}", artifact, version)

    path = adapter.write(proposal_text, version)
    ts = store.now_iso()
    rule_id = f"{ACTION}:{artifact}"
    mid = f"{rule_id}@{ts}"
    row = MutationRow(
        mutation_id=mid,
        rule_id=rule_id,
        verdict=HUMAN_PROMOTED,
        arm_a=f"v{max(1, version - 1)}",
        arm_b=f"v{version}",
        applied_at=ts,
        reverted=False,
        note=f"{payload.get('stamp', '')} [ack humano]",
        action=ACTION,
    )
    if not store.record_mutation(row):
        for k in range(2, 10):
            alt = f"{rule_id}@{ts}-{k}"
            if store.record_mutation(replace(row, mutation_id=alt)):
                mid = alt
                break
        else:
            return _refused("evento-duplicado", artifact, version)

    payload["applied_at"] = ts
    payload["applied_mutation_id"] = mid
    _write_atomic(d / EVIDENCE_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    return {
        "status": "ok",
        "reason": "",
        "path": str(path),
        "artifact": artifact,
        "version": version,
        "mutation_id": mid,
    }


def undo_queued(entry_id: str, *, root: Path | str | None = None, why: str = "manual undo") -> dict:
    """O undo da entrada HUMANA — `harness rollback` recusa `action !=
    "tune"` e restauraria de `data/tune/` (que o próximo run sobrescreve);
    aqui a fonte é o `incumbent.txt` da PRÓPRIA entrada."""
    d = _find_queue_entry(entry_id)
    if d is None:
        return _refused("id-desconhecido")
    payload = _load_queue_payload(d)
    if payload is None:
        return _refused("id-desconhecido")

    artifact = str(payload.get("artifact", ""))
    version = int(payload.get("version", 0))
    applied_mid = payload.get("applied_mutation_id")
    if not payload.get("applied_at") or not applied_mid:
        return _refused("nao-aplicada", artifact, version)

    try:
        proposal_text = (d / PROPOSAL_FILE).read_text(encoding="utf-8")
        incumbent_text = (d / INCUMBENT_FILE).read_text(encoding="utf-8")
    except OSError:
        return _refused("id-desconhecido", artifact, version)

    target = improve_root_dir(root) / artifact
    try:
        disk_text = target.read_text(encoding="utf-8")
    except OSError:
        disk_text = ""
    if _sha(disk_text) != _sha(proposal_text):
        return _refused("artefato-mudou-depois", artifact, version)

    _write_atomic(target, incumbent_text)

    ts = store.now_iso()
    rule_id = f"{ACTION}:{artifact}"
    mid = f"{rule_id}@{ts}"
    row = MutationRow(
        mutation_id=mid,
        rule_id=rule_id,
        verdict=UNDONE,
        arm_a=f"v{version}",
        arm_b=f"v{max(1, version - 1)}",
        applied_at=ts,
        reverted=False,
        note=f"why={why}; undoes={applied_mid}",
        action=ACTION,
    )
    if not store.record_rollback(row, undoes=str(applied_mid)):
        for k in range(2, 10):
            alt = f"{rule_id}@{ts}-{k}"
            if store.record_rollback(replace(row, mutation_id=alt), undoes=str(applied_mid)):
                mid = alt
                break
        else:
            return _refused("evento-duplicado", artifact, version)

    return {
        "status": "ok",
        "reason": "",
        "path": str(target),
        "artifact": artifact,
        "version": version,
        "mutation_id": mid,
    }


def history(limit: int = 50) -> list[MutationRow]:
    return [m for m in store.mutations(limit=None) if m.action == ACTION][:limit]
