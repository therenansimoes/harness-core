#!/usr/bin/env python3
"""Testa runplan.py — o plano da run é explícito, validado e retrocompatível.

Nenhum teste aqui chama backend, rede ou LLM: o plano é config + string. O que
precisa ser verdade: o tools.toml real cabe no teto imutável de safety, um kind
pedindo tool desconhecida DERRUBA o load (não filtra em silêncio), e o caminho
sem plano monta exatamente o comando de antes — retrocompat do run_task.py.

    python3 -m pytest tests/test_runplan.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import agent  # noqa: E402
import router  # noqa: E402
import runplan  # noqa: E402
import safety  # noqa: E402

TIER = router.Tier(
    name="haiku", rank=0, model="claude-haiku-4-5-20251001", max_turns=17, est_cost_per_run=0.02
)


def test_tools_subset_of_safety_max():
    """O tools.toml versionado não pode pedir nada fora do teto — se pedir, o
    load levanta, e é isso que este teste garante que não acontece hoje."""
    cfg = runplan.load_tools()
    for kind, section in cfg.items():
        assert set(section["tools"]) <= set(safety.ALLOWED_TOOLS_MAX), kind


def test_unknown_tool_raises(tmp_path):
    bad = tmp_path / "tools.toml"
    bad.write_text('[default]\ntools = ["Bash", "Rm"]\n', encoding="utf-8")
    with pytest.raises(runplan.RunPlanError) as e:
        runplan.load_tools(bad)
    assert "Rm" in str(e.value)


def test_missing_file_raises(tmp_path):
    with pytest.raises(runplan.RunPlanError):
        runplan.load_tools(tmp_path / "nao_existe.toml")


def test_default_plan_matches_legacy_tools(tmp_path):
    plan = runplan.build("default", TIER, tmp_path, "proj")
    assert plan.tools == agent.ALLOWED_TOOLS
    assert plan.system_prompt == agent._system_prompt(tmp_path)
    assert plan.max_turns == TIER.max_turns
    assert plan.mcp_config_path is None
    assert plan.skills == []
    assert plan.memory_digest == ""


def test_unknown_kind_falls_back_to_default():
    assert runplan.tools_for("kind_que_nao_existe") == runplan.tools_for("default")


def _capture_cmd(monkeypatch, workspace, plan):
    """Roda _run_cli com o subprocess trocado por um espião e devolve o argv."""
    captured = {}

    def fake_safe_run(cmd, cwd, timeout, env=None):
        captured["cmd"] = list(cmd)
        return 0, "", ""

    monkeypatch.setattr(agent.safety, "safe_run", fake_safe_run)
    agent._run_cli("prompt qualquer", workspace, plan)
    return captured["cmd"]


def test_plan_none_matches_legacy_cmd(monkeypatch, tmp_path):
    """plan=None precisa montar o MESMO comando de antes do RunPlan existir."""
    legacy = [
        "claude",
        "-p",
        "prompt qualquer",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        agent._model(),
        "--max-turns",
        str(agent._max_turns()),
        "--allowed-tools",
        *agent.ALLOWED_TOOLS,
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        agent._system_prompt(tmp_path),
    ]
    assert _capture_cmd(monkeypatch, tmp_path, None) == legacy


def test_plan_drives_cmd(monkeypatch, tmp_path):
    """Com plano, tools/turns/system prompt saem do plano, não das globais."""
    plan = runplan.RunPlan(
        system_prompt="PROMPT DO PLANO",
        tools=["Read", "Grep"],
        max_turns=7,
    )
    cmd = _capture_cmd(monkeypatch, tmp_path, plan)
    i = cmd.index("--allowed-tools")
    assert cmd[i + 1 : i + 3] == ["Read", "Grep"]
    assert cmd[cmd.index("--max-turns") + 1] == "7"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "PROMPT DO PLANO"
    assert "Write" not in cmd and "Bash" not in cmd
