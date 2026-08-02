#!/usr/bin/env python3
"""Testa agent.py:_run_cli — sem chamada real ao `claude` (safety.safe_run
é monkeypatchado com stdout/stderr/returncode sintéticos).

    python3 -m pytest tests/test_agent.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import agent  # noqa: E402


def _patch_safe_run(monkeypatch, returncode: int, stdout: str, stderr: str):
    def fake_safe_run(cmd, cwd, timeout, env=None):
        return returncode, stdout, stderr

    monkeypatch.setattr(agent.safety, "safe_run", fake_safe_run)


def test_max_turns_com_exit_1_preserva_tokens_e_custo(monkeypatch, tmp_path):
    """`claude -p --output-format json` sai com exit!=0 sempre que
    is_error=true — inclusive em estourar --max-turns, que ainda carrega
    usage/cost reais. Isso não pode virar 0 tokens/$0."""
    payload = {
        "is_error": True,
        "subtype": "error_max_turns",
        "num_turns": 13,
        "total_cost_usd": 0.0881848,
        "usage": {"input_tokens": 161, "output_tokens": 1955},
        "result": "",
    }
    _patch_safe_run(monkeypatch, returncode=1, stdout=json.dumps(payload), stderr="")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is False  # is_error=true -> a task não foi concluída
    assert res.tokens == 161 + 1955
    assert res.cost_usd == 0.0881848
    assert res.turns == 13
    assert res.notes == "error_max_turns"


def test_cli_exit_sem_json_nenhum_e_infra_error(monkeypatch, tmp_path):
    """Crash de infra de verdade: nada no stdout pra parsear -> cli_exit_N,
    0 tokens (não há o que recuperar)."""
    _patch_safe_run(monkeypatch, returncode=1, stdout="", stderr="")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is False
    assert res.tokens == 0
    assert res.notes.startswith("cli_exit_1")


def test_exit_0_com_json_valido_continua_funcionando(monkeypatch, tmp_path):
    payload = {
        "is_error": False,
        "num_turns": 1,
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 10, "output_tokens": 51},
        "result": "DONE: ok",
    }
    _patch_safe_run(monkeypatch, returncode=0, stdout=json.dumps(payload), stderr="")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is True
    assert res.tokens == 61
    assert res.notes == ""
