"""Verify: a régua roda o comando do alvo e devolve um Verdict.

O agente não declara que passou — o exit code declara. O log fica FORA do
workspace (`$HARNESS_DATA_DIR/logs/<run_id>/`): dentro do ws o retry leria o
golden impresso pelo verificador selado e passaria a régua por decoração.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from harness.ledger import store
from harness.types import Check, UnitSpec, Verdict

LOG_REL = Path(".harness") / "verify.log"
LOGS_REL = "logs"
DEFAULT_TIMEOUT_S = 300.0
TAIL_LINES = 15  # diagnóstico: o fim do log é onde o erro costuma estar
TIMEOUT_EXIT = 124  # convenção do `timeout(1)`
NOEXEC_EXIT = 127  # convenção do shell para "não deu para executar"

# Régua graduada: o `verify_cmd` é o check implícito de peso 1.0 que sempre
# entra no score. Nome reservado — nenhum `[checks]` pode se chamar assim.
VERIFY_CHECK_NAME = "verify_cmd"
VERIFY_CHECK_WEIGHT = 1.0
PER_CHECK_TIMEOUT_S = 60.0
CHECKS_EXIT = 125  # veredito derrubado por SUBCHECK, com o `verify_cmd` verde


def run_log_dir(run_id: str, data_dir: Path | str | None = None) -> Path:
    """Diretório dos logs deste run, fora de qualquer workspace.

    Absoluto de propósito: `HARNESS_DATA_DIR` relativo mais o cwd do subprocess
    do verify daria caminhos diferentes para o mesmo run.
    """
    base = Path(data_dir) if data_dir is not None else store.data_dir()
    path = (base / LOGS_REL / run_id).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_extra_checks(
    checks: tuple[Check, ...],
    ws: Path,
    *,
    per_check_timeout_s: float = PER_CHECK_TIMEOUT_S,
    budget_s: float,
) -> tuple[float, tuple[str, ...], str]:
    """Roda os `[checks]` da unidade e devolve `(score, reprovados, log)`.

    `score` é só sobre estes checks (`Σpeso(passou)/Σpeso`); quem soma o
    `verify_cmd` implícito é `graded_score`. Roda TODOS, em ordem, sem nunca
    curto-circuitar: o valor de uma régua graduada é justamente saber quanto
    passou, e parar no primeiro vermelho devolveria a mesma informação binária
    de antes.

    Sem check nenhum devolve `(1.0, (), "")` — o caller nem precisa saber.
    Orçamento estourado NÃO é aprovação: o que não rodou entra em `reprovados`,
    e o log diz quantos foram, senão um score baixo por timeout viraria
    diagnóstico de código errado.
    """
    if not checks:
        return 1.0, (), ""
    total = sum(c.weight for c in checks)
    got = 0.0
    failed: list[str] = []
    parts: list[str] = []
    not_run = 0
    t0 = time.monotonic()
    for check in checks:
        left = budget_s - (time.monotonic() - t0)
        if left <= 0:
            failed.append(check.name)
            not_run += 1
            continue
        limit = min(per_check_timeout_s, left)
        try:
            proc = subprocess.run(
                check.cmd,
                shell=True,
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=limit,
            )
            exit_code = proc.returncode
            out = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = TIMEOUT_EXIT
            out = _text(exc.stdout) + _text(exc.stderr) + f"\ncheck: timeout após {limit:.0f}s\n"
        except OSError as exc:
            exit_code = NOEXEC_EXIT
            out = f"check: não executou — {exc}\n"
        parts.append(f"=== {check.name} exit={exit_code} ===\n{out}")
        if exit_code == 0:
            got += check.weight
        else:
            failed.append(check.name)
    if not_run:
        parts.append(f"checks: orçamento estourado, {not_run} não rodaram\n")
    return (got / total if total > 0 else 1.0), tuple(failed), "".join(parts)


def graded_score(verify_ok: bool, checks: tuple[Check, ...], extra_score: float) -> float:
    """Score da régua inteira: o `verify_cmd` é um check implícito de peso 1.0.

    Sem `[checks]` não existe régua graduada — devolve 1.0 e o veredito continua
    sendo o binário de sempre (`passed` do exit code).
    """
    if not checks:
        return 1.0
    extra_weight = sum(c.weight for c in checks)
    total = VERIFY_CHECK_WEIGHT + extra_weight
    got = (VERIFY_CHECK_WEIGHT if verify_ok else 0.0) + extra_score * extra_weight
    return got / total if total > 0 else 1.0


def run_verify(
    unit: UnitSpec,
    ws: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    log_dir: Path | None = None,
) -> Verdict:
    """Roda `unit.verify_cmd` com `ws` de cwd; stdout+stderr vão para o log.

    Timeout e falha de execução viram exit code próprio em vez de exceção: o
    gate precisa de um veredito, não de um traceback.
    `log_dir` (o de `run_log_dir`) tira o log do workspace; sem ele o log cai em
    `ws / LOG_REL`, que é o caminho de quem só quer um veredito solto.

    `unit.checks` (opcional) roda DEPOIS do comando principal, no mesmo log, e
    preenche `score`/`failed`. Sem eles o Verdict é o de sempre, campo a campo.
    """
    log_path = (log_dir / "verify.log") if log_dir is not None else ws / LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            unit.verify_cmd,
            shell=True,
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        exit_code = proc.returncode
        log = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = TIMEOUT_EXIT
        log = _text(exc.stdout) + _text(exc.stderr) + f"\nverify: timeout após {timeout_s}s\n"
    except OSError as exc:
        exit_code = NOEXEC_EXIT
        log = f"verify: não executou — {exc}\n"
    # Régua graduada: orçamento próprio (o `timeout_s` é do comando principal),
    # e o vermelho do comando principal não dispensa os checks — é justamente
    # quando saber quanto passou vale algo.
    extra_score, failed, checks_log = run_extra_checks(unit.checks, ws, budget_s=timeout_s)
    if checks_log:
        log += checks_log
    verify_ok = exit_code == 0
    if unit.checks and failed and verify_ok:
        exit_code = CHECKS_EXIT
    score = graded_score(verify_ok, unit.checks, extra_score)
    sec = time.monotonic() - t0
    log_path.write_text(log, encoding="utf-8")
    return Verdict(
        passed=exit_code == 0,
        exit_code=exit_code,
        log_path=log_path,
        sec=sec,
        score=score,
        failed=failed,
    )


def log_tail(log_path: Path, lines: int = TAIL_LINES) -> str:
    """Últimas `lines` linhas úteis do log do verify, para quem só vê o exit code.

    Log ausente/ilegível devolve string vazia: diagnóstico é bônus, nunca motivo
    de o run morrer depois de já ter um veredito.
    """
    try:
        raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    kept = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])


def _text(raw: object) -> str:
    """TimeoutExpired entrega bytes mesmo com `text=True`."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)
