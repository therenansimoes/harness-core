#!/usr/bin/env python3
"""Testa agent.py:_run_cli e o trace.jsonl (SPEC-J2 design 1) — sem chamada
real ao `claude` (safety.safe_run é monkeypatchado com stdout/stderr/
returncode sintéticos) e sem tocar em runs/ do repo (HARNESS_TRACE_ROOT
sempre apontado pra tmp_path nos testes).

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


def _result_event(**overrides) -> dict:
    base = {
        "type": "result",
        "is_error": False,
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "result": "",
    }
    base.update(overrides)
    return base


def _stream(events: list[dict]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


def test_max_turns_com_exit_1_preserva_tokens_e_custo(monkeypatch, tmp_path):
    """`claude -p --output-format stream-json` sai com exit!=0 sempre que
    is_error=true no evento result — inclusive em estourar --max-turns, que
    ainda carrega usage/cost reais. Isso não pode virar 0 tokens/$0. Stream
    de 5 linhas (aceite 1 do SPEC-J2): 4 eventos de trabalho + 1 result."""
    monkeypatch.setenv("HARNESS_TRACE_ROOT", str(tmp_path / "runs"))
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "turno 1"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "turno final"}]}},
        _result_event(
            is_error=True,
            subtype="error_max_turns",
            num_turns=13,
            total_cost_usd=0.0881848,
            usage={"input_tokens": 161, "output_tokens": 1955},
            result="",
        ),
    ]
    _patch_safe_run(monkeypatch, returncode=1, stdout=_stream(events), stderr="")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is False  # is_error=true -> a task não foi concluída
    assert res.tokens == 161 + 1955
    assert res.cost_usd == 0.0881848
    assert res.turns == 13
    assert res.notes == "error_max_turns"
    # trace.jsonl gravado com as 5 linhas do stream, na ordem original.
    assert res.trace_lines == 5
    assert res.trace_path
    trace_file = Path(res.trace_path)
    assert trace_file.exists()
    lines = trace_file.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[-1])["type"] == "result"


def test_stream_sem_result_e_rc_1_vira_cli_exit_1(monkeypatch, tmp_path):
    """Aceite 2 do SPEC-J2: stream com eventos mas nenhum type=='result' +
    returncode!=0 -> cli_exit_N (infra quebrou antes do harness fechar a run,
    não há number pra recuperar)."""
    monkeypatch.setenv("HARNESS_TRACE_ROOT", str(tmp_path / "runs"))
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "trabalhando"}]}},
    ]
    _patch_safe_run(monkeypatch, returncode=1, stdout=_stream(events), stderr="crash de infra")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is False
    assert res.tokens == 0
    assert res.notes.startswith("cli_exit_1")
    # o stream parcial ainda vira trace, mesmo sem result — útil pra debug.
    assert res.trace_lines == 2


def test_cli_exit_sem_json_nenhum_e_infra_error(monkeypatch, tmp_path):
    """Crash de infra de verdade: nada no stdout pra parsear -> cli_exit_N,
    0 tokens (não há o que recuperar), sem trace."""
    monkeypatch.setenv("HARNESS_TRACE_ROOT", str(tmp_path / "runs"))
    _patch_safe_run(monkeypatch, returncode=1, stdout="", stderr="")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is False
    assert res.tokens == 0
    assert res.notes.startswith("cli_exit_1")
    assert res.trace_path == ""
    assert res.trace_lines == 0


def test_exit_0_com_json_valido_continua_funcionando(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_TRACE_ROOT", str(tmp_path / "runs"))
    events = [
        {"type": "system", "subtype": "init"},
        _result_event(
            num_turns=1,
            total_cost_usd=0.02,
            usage={"input_tokens": 10, "output_tokens": 51},
            result="DONE: ok",
        ),
    ]
    _patch_safe_run(monkeypatch, returncode=0, stdout=_stream(events), stderr="")

    res = agent._run_cli("faça algo", tmp_path)

    assert res.ok is True
    assert res.tokens == 61
    assert res.notes == ""


def test_900_eventos_trunca_pra_400_com_marcador_na_posicao_101(monkeypatch, tmp_path):
    """Aceite 3 do SPEC-J2: 900 eventos -> exatamente TRACE_MAX_LINES (400)
    linhas gravadas, linha 101 (índice 100) é o marcador harness_trunc, e
    toda linha do arquivo final é JSON parseável."""
    monkeypatch.setenv("HARNESS_TRACE_ROOT", str(tmp_path / "runs"))
    lines_in = [json.dumps({"type": "assistant", "seq": i}) for i in range(900)]

    trace_path, n_lines = agent._write_trace(lines_in, run_id="run_900")

    assert n_lines == agent.TRACE_MAX_LINES == 400
    trace_file = tmp_path / "runs" / "run_900" / "trace.jsonl"
    out_lines = trace_file.read_text().splitlines()
    assert len(out_lines) == 400
    for line in out_lines:
        json.loads(line)  # tudo parseável
    marker = json.loads(out_lines[100])
    assert marker == {"type": "harness_trunc", "dropped": 900 - 100 - 299}
    assert json.loads(out_lines[0])["seq"] == 0
    assert json.loads(out_lines[-1])["seq"] == 899


def test_campo_grande_e_truncado_deterministicamente(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_TRACE_ROOT", str(tmp_path / "runs"))
    big = "x" * (agent.TRACE_MAX_FIELD + 500)
    lines_in = [json.dumps({"type": "assistant", "text": big})]

    trace_path, n_lines = agent._write_trace(lines_in, run_id="run_big_field")

    assert n_lines == 1
    out = (tmp_path / "runs" / "run_big_field" / "trace.jsonl").read_text().splitlines()
    evt = json.loads(out[0])
    assert len(evt["text"]) < len(big)
    assert evt["text"].endswith("…[trunc 500 chars]")
