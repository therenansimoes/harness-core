#!/usr/bin/env python3
"""Testa judges/process_metrics.py (SPEC-J2 design 2, trilha PROCESSO) e o
brief.md de benchmarks/judge/build_j_b2b/ (trilha B).

Sem chamada a LLM — process_metrics.py é puro stdlib.

    python3 -m pytest tests/test_process_metrics.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "attic" / "judges"))

import process_metrics as pm  # noqa: E402

# --------------------------------------------------------- trace sintético


def _assistant(msg_id: str, blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"id": msg_id, "content": blocks}}


def _tool_use(tool_use_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}


def _user_result(tool_use_id: str, is_error: bool, content: str) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                    "content": content,
                }
            ]
        },
    }


def _result_event(**kw) -> dict:
    base = {"type": "result", "is_error": False, "num_turns": 5}
    base.update(kw)
    return base


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _synthetic_3_erros_2_recuperacoes(tmp_path: Path) -> Path:
    """3 chamadas Bash idênticas ("pytest tests/foo.py") falham com o mesmo
    erro (turnos 1-3, dispara thrash — ver THRASH_THRESHOLD). 1 chamada
    diferente ("pytest tests/foo.py -v") no mesmo alvo ("pytest") no turno 5
    tem sucesso e cai dentro da janela de recuperação (<=3 turnos) dos erros
    dos turnos 2 e 3, mas NÃO do turno 1 (1+3=4 < 5) — 2 recuperações."""
    err_cmd = {"command": "pytest tests/foo.py"}
    fix_cmd = {"command": "pytest tests/foo.py -v"}
    events = [
        _assistant("m1", [_tool_use("t1", "Bash", err_cmd)]),
        _user_result("t1", True, "ImportError: no module named foo"),
        _assistant("m2", [_tool_use("t2", "Bash", err_cmd)]),
        _user_result("t2", True, "ImportError: no module named foo"),
        _assistant("m3", [_tool_use("t3", "Bash", err_cmd)]),
        _user_result("t3", True, "ImportError: no module named foo"),
        _assistant("m4", [{"type": "text", "text": "deixa eu conferir o import"}]),
        _assistant("m5", [_tool_use("t5", "Bash", fix_cmd)]),
        _user_result("t5", False, "1 passed"),
        _result_event(num_turns=5),
    ]
    return _write_jsonl(tmp_path / "trace.jsonl", events)


def test_aceite1_x2_igual_a_4_sem_llm(tmp_path):
    trace = _synthetic_3_erros_2_recuperacoes(tmp_path)
    metrics = pm.parse_trace(trace)

    assert metrics["n_tool_errors"] == 3
    assert metrics["n_recovered"] == 2
    assert metrics["n_thrash"] == 1  # 3x mesma chamada, mesmo erro

    assert pm.X2(metrics) == 4  # 10*2/3 - 3 = 3.667 -> round == 4


def test_aceite2_zero_erros_x2_discarded(tmp_path):
    events = [
        _assistant("m1", [_tool_use("t1", "Bash", {"command": "pytest"})]),
        _user_result("t1", False, "1 passed"),
        _result_event(num_turns=1),
    ]
    trace = _write_jsonl(tmp_path / "trace.jsonl", events)

    metrics = pm.parse_trace(trace)

    assert metrics["n_tool_errors"] == 0
    assert pm.X2(metrics) == pm.DISCARDED


def test_x1_sucesso_sem_ajuda_e_dez():
    metrics = {"stop_reason": "success", "n_help_requests": 0}
    assert pm.X1(metrics) == 10


def test_x1_max_turns_e_zero():
    metrics = {"stop_reason": "max_turns", "n_help_requests": 0}
    assert pm.X1(metrics) == 0


def test_x1_desconta_por_pedido_de_ajuda():
    metrics = {"stop_reason": "success", "n_help_requests": 2}
    assert pm.X1(metrics) == 2  # 10 - 4*2


# ------------------------------------------------------- traces reais (fixture)


def test_trace_real_limpo_sem_erros():
    """runs/harness_task_01_80qjfgur/trace.jsonl — 2 turnos, sem erro,
    stop_reason end_turn/is_error False."""
    metrics = pm.parse_trace(FIXTURES / "trace_real_clean.jsonl")

    assert metrics["n_turns"] == 2
    assert metrics["n_tool_calls"] == 1
    assert metrics["n_tool_errors"] == 0
    assert metrics["stop_reason"] == "success"
    assert pm.X2(metrics) == pm.DISCARDED
    assert pm.X1(metrics) == 10


def test_trace_real_com_erros_e_max_turns():
    """runs/harness_task_j_b2b_765nxvc8/trace.jsonl — trace real com 3
    tool_result de erro e subtype=error_max_turns no evento result."""
    metrics = pm.parse_trace(FIXTURES / "trace_real_errors.jsonl")

    assert metrics["n_turns"] == 13
    assert metrics["n_tool_errors"] == 3
    assert metrics["n_recovered"] == 2
    assert metrics["stop_reason"] == "max_turns"
    assert pm.X1(metrics) == 0  # trava em max_turns, independente de ajuda


# ------------------------------------------------------------------ X3 (D4)


def test_x3_sem_baseline_e_cinco_por_default():
    assert pm.X3(turns=10, cost=1.0, baseline_median=None) == 5.0


def test_x3_abaixo_da_mediana_e_cinco():
    assert pm.X3(turns=5, cost=0.5, baseline_median={"turns": 10, "cost_usd": 1.0}) == 5.0


def test_x3_dobro_da_mediana_e_zero():
    assert pm.X3(turns=20, cost=2.0, baseline_median={"turns": 10, "cost_usd": 1.0}) == 0.0


# ----------------------------------------------------------- brief.md (trilha B)


def test_brief_minimo():
    """Aceite 3 do SPEC-J2 design 2: brief.md tem <=25 linhas e as 3 seções
    obrigatórias (## Goal, ## Regras, ## Dicas)."""
    brief = REPO / "benchmarks" / "judge" / "build_j_b2b" / "brief.md"
    lines = brief.read_text().splitlines()

    assert len(lines) <= 25, f"brief.md tem {len(lines)} linhas, limite é 25"
    text = "\n".join(lines)
    assert "## Goal" in text
    assert "## Regras" in text
    assert "## Dicas" in text
