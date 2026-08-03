"""run_id viaja no ExecRequest — sem ele a atribuição de skills grava
session_id e o join com a tabela runs não fecha."""

from pathlib import Path
from typing import ClassVar

import pytest

from harness.backends import registry
from harness.graph.run_graph import run_unit
from harness.types import Capabilities, ExecRequest, ExecResult, Preflight

FIXTURE = Path(__file__).parent / "fixtures" / "echo"
OUTPUT = "spy.txt"


def test_exec_request_run_id_default_none(tmp_path):
    req = ExecRequest(prompt="x", workspace=tmp_path)
    assert req.run_id is None


class RunIdSpy:
    """Captura cada ExecRequest recebido — torna o run_id observável."""

    name: ClassVar[str] = "runid-spy"

    def __init__(self, requests: list[ExecRequest]) -> None:
        self.requests = requests

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
        self.requests.append(req)
        req.workspace.mkdir(parents=True, exist_ok=True)
        (req.workspace / OUTPUT).write_text("x", encoding="utf-8")
        return ExecResult(
            ok=True,
            exit_reason="done",
            turns=1,
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            files_changed=(OUTPUT,),
            session_id=None,
            trace_path=req.trace_path,
        )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def spy():
    requests: list[ExecRequest] = []
    registry.register("runid-spy", lambda: RunIdSpy(requests))
    yield requests
    registry.unregister("runid-spy")


def test_run_graph_passes_run_id_to_backend(data_dir, spy):
    run_unit(FIXTURE, "runid-spy", None, data_dir, thread_id="t-runid")

    assert spy, "o backend precisa ter sido chamado"
    assert all(req.run_id == "t-runid" for req in spy)
