"""Verify: a régua roda o comando do alvo e devolve um Verdict.

O agente não declara que passou — o exit code declara. O log fica dentro do
workspace (`.harness/verify.log`) para viajar junto com o run.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from harness.types import UnitSpec, Verdict

LOG_REL = Path(".harness") / "verify.log"
DEFAULT_TIMEOUT_S = 300.0
TAIL_LINES = 15      # diagnóstico: o fim do log é onde o erro costuma estar
TIMEOUT_EXIT = 124   # convenção do `timeout(1)`
NOEXEC_EXIT = 127    # convenção do shell para "não deu para executar"


def run_verify(unit: UnitSpec, ws: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> Verdict:
    """Roda `unit.verify_cmd` com `ws` de cwd; stdout+stderr vão para o log.

    Timeout e falha de execução viram exit code próprio em vez de exceção: o
    gate precisa de um veredito, não de um traceback.
    """
    log_path = ws / LOG_REL
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
