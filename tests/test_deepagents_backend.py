import builtins
import json
from pathlib import Path

import pytest

from harness.backends import deepagents_backend as da
from harness.types import ExecRequest

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_fix"


@pytest.fixture
def no_deepagents(monkeypatch):
    """Simula o extra desinstalado — o teste não pode depender do ambiente."""
    real_import = builtins.__import__

    def fake(name, *a, **kw):
        if name == "deepagents" or name.startswith("deepagents."):
            raise ImportError("No module named 'deepagents'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_preflight_without_lib_is_not_ok(no_deepagents):
    pre = da.DeepagentsBackend().preflight()
    assert pre.ok is False
    assert "harness-core[deepagents]" in pre.reason


def test_execute_without_lib_is_blocked(no_deepagents, tmp_path):
    req = ExecRequest(prompt="x", workspace=tmp_path, trace_path=tmp_path / "trace.jsonl")
    res = da.DeepagentsBackend().execute(req)
    assert (res.ok, res.exit_reason, res.files_changed) == (False, "blocked", ())
    assert "deepagents" in json.loads(res.trace_path.read_text())["error"]


def test_preflight_reports_ollama_down(monkeypatch):
    monkeypatch.setattr(da, "_import_deepagents", lambda: None)
    monkeypatch.setattr(da, "OLLAMA_URL", "http://localhost:1/api/tags")
    pre = da.DeepagentsBackend(model="ollama:qwen3:4b").preflight()
    assert pre.ok is False
    assert "Ollama" in pre.reason


def test_capabilities_are_declared():
    caps = da.DeepagentsBackend().capabilities()
    assert caps.model_selectable is True
    assert "edit_file" in caps.tools


# --- pricing ---------------------------------------------------------------


def test_pricing_file_has_only_free_local_models():
    pricing = da.load_pricing(Path("config"))
    assert pricing, "config/models.toml sem [pricing]"
    # locais grátis: ollama:* e os modelos MLX servidos pelo LM Studio em loopback
    local_openai = {"openai:qwen3.5-9b-optiq", "openai:qwen3.5-9b-mlx"}
    assert all(k.startswith("ollama:") or k in local_openai for k in pricing)
    assert all(
        v.get("input_per_mtok") == 0.0 and v.get("output_per_mtok") == 0.0
        for v in pricing.values()
    )


def test_cost_ollama_is_zero_even_off_table():
    assert da.cost_usd("ollama:qwen3:4b", 1000, 1000, pricing={}) == 0.0


def test_cost_unknown_model_is_none():
    assert da.cost_usd("gpt-mistério", 1000, 1000, pricing={}) is None
    assert da.cost_usd(None, 0, 0, pricing={}) is None


def test_cost_uses_table_per_mtok():
    pricing = {"acme:big": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}
    assert da.cost_usd("acme:big", 1_000_000, 2_000_000, pricing=pricing) == 33.0


# --- files_changed ---------------------------------------------------------


def test_snapshot_diff_detects_write_edit_and_delete(tmp_path):
    (tmp_path / "sub").mkdir()
    keep = tmp_path / "keep.txt"
    keep.write_text("k")
    gone = tmp_path / "sub" / "gone.txt"
    gone.write_text("g")
    before = da._snapshot(tmp_path)

    gone.unlink()
    (tmp_path / "novo.txt").write_text("n")
    keep.write_text("keep mudou")

    assert da._diff(before, da._snapshot(tmp_path)) == ("keep.txt", "novo.txt", "sub/gone.txt")


def test_snapshot_ignores_trace_file(tmp_path):
    trace = tmp_path / "trace.jsonl"
    before = da._snapshot(tmp_path, exclude=trace)
    trace.write_text("{}\n")
    assert da._diff(before, da._snapshot(tmp_path, exclude=trace)) == ()


def test_timeout_helper_gives_up(tmp_path):
    import time

    with pytest.raises(TimeoutError):
        da._with_timeout(lambda: time.sleep(5), 0.05)
    assert da._with_timeout(lambda: 42, 5) == 42


# --- allowlist de tools (precisa da lib, não da rede) ----------------------


def _agent_tool_names(agent) -> set[str]:
    return set(agent.nodes["tools"].bound.tools_by_name)


def test_allowlist_intersects_only_filesystem_tools():
    pytest.importorskip("deepagents")
    assert da._fs_allowlist(("ls", "read_file", "note_write")) == ["ls", "read_file"]
    assert da._fs_allowlist(("note_write",)) == []
    assert da._fs_allowlist(()) == []


def test_middleware_replacement_actually_restricts_tools(tmp_path):
    """Pendência 1 do RESEARCH: `FilesystemMiddleware` via `middleware=` substitui."""
    pytest.importorskip("deepagents")
    base = ExecRequest(prompt="x", workspace=tmp_path, model="ollama:qwen3:4b", max_turns=3)

    default, _ = da._build_agent(base)
    assert {"write_file", "execute", "delete"} <= _agent_tool_names(default)

    from dataclasses import replace

    restrito, _ = da._build_agent(replace(base, tools=("ls", "read_file", "edit_file")))
    assert "execute" not in _agent_tool_names(restrito)
    assert {"ls", "read_file", "edit_file"} <= _agent_tool_names(restrito)


# --- ollama de verdade -----------------------------------------------------


@pytest.mark.ollama
def test_e2e_tiny_fix_with_ollama(tmp_path):
    from harness import cli

    model = "ollama:qwen2.5:3b"
    unit = cli.load_unit(FIXTURE)
    cli.seed_workspace(unit, tmp_path)
    backend = da.DeepagentsBackend(model=model)
    assert backend.preflight().ok, backend.preflight().reason

    res = backend.execute(
        ExecRequest(
            prompt=unit.prompt,
            workspace=tmp_path,
            model=model,
            max_turns=8,
            timeout_s=300.0,
            trace_path=tmp_path / "trace.jsonl",
        )
    )
    assert res.cost_usd == 0.0
    assert "target.py" in res.files_changed
    assert "a + b" in (tmp_path / "target.py").read_text()
