"""Helper do test_resume: backend-sonda + child que morre no meio do `execute`.

Rodado de dois jeitos: como script (`python resume_child.py ...`, modo lento, é
o processo que leva SIGKILL) e importado pelo teste (modo rápido, a retomada).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import ClassVar

from harness.backends.mock import OUTPUT_FILE
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

BACKEND_NAME = "resume_probe"
SENTINEL = "execute_started"
CALLS = "execute_calls"
DONE = "execute_done"
SLOW_SLEEP_S = 5.0
# Mesmo arquivo de saída do mock: assim a sonda reusa o unit `echo` e seu verify.


class ProbeBackend:
    """Deixa rastro em disco de cada chamada e de cada chamada *concluída*."""

    name: ClassVar[str] = "resume_probe"

    def __init__(self, marks: Path, sleep_s: float = 0.0) -> None:
        self.marks = marks
        self.sleep_s = sleep_s

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="sonda de teste")

    def execute(self, req: ExecRequest) -> ExecResult:
        self.marks.mkdir(parents=True, exist_ok=True)
        _append(self.marks / CALLS, os.getpid())
        (self.marks / SENTINEL).write_text(str(os.getpid()), encoding="utf-8")
        if self.sleep_s:
            time.sleep(self.sleep_s)  # janela determinística para o SIGKILL

        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / OUTPUT_FILE).write_text(req.prompt, encoding="utf-8")
        _append(self.marks / DONE, os.getpid())
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(OUTPUT_FILE,),
            session_id=None,
            trace_path=req.trace_path,
        )


def _append(path: Path, pid: int) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{pid}\n")
        fh.flush()
        os.fsync(fh.fileno())  # o processo pode morrer no instante seguinte


def count(marks: Path, name: str) -> int:
    path = marks / name
    if not path.is_file():
        return 0
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln])


def register(marks: Path, sleep_s: float) -> None:
    from harness.backends import registry

    registry.register(BACKEND_NAME, lambda: ProbeBackend(marks, sleep_s))


def main(argv: list[str]) -> int:
    marks, data_dir, unit_dir, thread_id = argv
    os.environ["HARNESS_DATA_DIR"] = data_dir
    register(Path(marks), SLOW_SLEEP_S)

    from harness.graph.run_graph import run_unit

    run_unit(Path(unit_dir), BACKEND_NAME, None, Path(data_dir), thread_id)
    return 0  # inalcançável no teste: o SIGKILL chega durante o `execute`


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
