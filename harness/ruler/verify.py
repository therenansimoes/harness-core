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
from harness.types import UnitSpec, Verdict

LOG_REL = Path(".harness") / "verify.log"
LOGS_REL = "logs"
DEFAULT_TIMEOUT_S = 300.0
TAIL_LINES = 15      # diagnóstico: o fim do log é onde o erro costuma estar
TIMEOUT_EXIT = 124   # convenção do `timeout(1)`
NOEXEC_EXIT = 127    # convenção do shell para "não deu para executar"


def run_log_dir(run_id: str, data_dir: Path | str | None = None) -> Path:
    """Diretório dos logs deste run, fora de qualquer workspace.

    Absoluto de propósito: `HARNESS_DATA_DIR` relativo mais o cwd do subprocess
    do verify daria caminhos diferentes para o mesmo run.
    """
    base = Path(data_dir) if data_dir is not None else store.data_dir()
    path = (base / LOGS_REL / run_id).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    """
    log_path = (log_dir / "verify.log") if log_dir is not None else ws / LOG_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            unit.verify_cmd, shell=True, cwd=str(ws),
            capture_output=True, text=True, timeout=timeout_s,
        )
        exit_code = proc.returncode
        log = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = TIMEOUT_EXIT
        log = _text(exc.stdout) + _text(exc.stderr) + f"\nverify: timeout após {timeout_s}s\n"
    except OSError as exc:
        exit_code = NOEXEC_EXIT
        log = f"verify: não executou — {exc}\n"
    sec = time.monotonic() - t0
    log_path.write_text(log, encoding="utf-8")
    return Verdict(passed=exit_code == 0, exit_code=exit_code, log_path=log_path, sec=sec)


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
