from harness.backends.mock import OUTPUT_FILE, MockBackend
from harness.types import ExecRequest


def _req(ws):
    return ExecRequest(prompt="olá mundo", workspace=ws, trace_path=ws / "trace.jsonl")


def test_execute_writes_output(tmp_path):
    result = MockBackend().execute(_req(tmp_path))

    out = tmp_path / OUTPUT_FILE
    assert out.is_file()
    assert out.read_text(encoding="utf-8") == "olá mundo"
    assert result.ok is True
    assert result.exit_reason == "done"
    assert result.turns == 1
    assert result.cost_usd == 0.0
    assert result.files_changed == (OUTPUT_FILE,)


def test_execute_is_deterministic(tmp_path):
    a = MockBackend().execute(_req(tmp_path / "a"))
    b = MockBackend().execute(_req(tmp_path / "b"))
    assert (a.ok, a.exit_reason, a.turns, a.cost_usd) == (b.ok, b.exit_reason, b.turns, b.cost_usd)


def test_capabilities_and_preflight():
    backend = MockBackend()
    caps = backend.capabilities()
    assert caps.reports_cost is True
    assert caps.resumable is False
    assert backend.preflight().ok is True
