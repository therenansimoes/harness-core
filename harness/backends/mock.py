"""Backend determinístico. Não chama LLM, não lê rede — existe para o teste."""

from __future__ import annotations

from typing import ClassVar

from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

OUTPUT_FILE = "mock_output.txt"


class MockBackend:
    name: ClassVar[str] = "mock"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=False,
            reports_cost=True,
            model_selectable=False,
            tools=frozenset({"write"}),
            streaming=False,
        )

    def preflight(self) -> Preflight:
        return Preflight(ok=True, reason="mock sempre disponível")

    def execute(self, req: ExecRequest) -> ExecResult:
        req.workspace.mkdir(parents=True, exist_ok=True)
        out = req.workspace / OUTPUT_FILE
        out.write_text(req.prompt, encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(OUTPUT_FILE,),
            session_id=req.session_id,
            trace_path=req.trace_path,
        )
