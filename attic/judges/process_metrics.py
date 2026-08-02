#!/usr/bin/env python3
"""process_metrics.py — métricas de "processo" (SPEC-J2 design 2) a partir de
um `trace.jsonl` já gravado (design 1). Puro stdlib, sem chamada a LLM: a
dimensão PROCESSO da RUBRIC-J2 (X1/X2/X3) é determinística — a persona (P3/P4)
só qualifica *como*, não decide o número.

Formato de entrada: stream-json do `claude -p --output-format stream-json`,
1 evento por linha (ver DESIGN 1 do SPEC-J2). Eventos relevantes:
  - `type=="assistant"`: `message.id` agrupa blocos (`thinking`/`text`/
    `tool_use`) da MESMA resposta — vira 1 "turno". `message.content` é uma
    lista de blocos.
  - `type=="user"`: quando `message.content` tem bloco `tool_result`, é a
    resposta de uma `tool_use` anterior (casada por `tool_use_id`).
    `is_error` é `True` em erro, `False`/ausente em sucesso.
  - `type=="result"`: último evento, 1x por trace — carrega `is_error`,
    `num_turns`, `stop_reason` (bruto da CLI) e opcionalmente `subtype`
    (ex.: `error_max_turns`).

"Alvo" de uma chamada (para casar erro→recuperação) é heurístico: para
Read/Edit/Write/NotebookEdit é o `file_path`/`notebook_path`; para Bash é o
primeiro token do comando (o binário/programa). "Chamada" (para thrash) é a
assinatura completa (tool + input serializado) — mesma chamada, não só mesmo
alvo.

"Pedido de ajuda" não tem canal próprio no protocolo `-p` (ALLOWED_TOOLS não
inclui nenhuma ferramenta de pergunta) — é inferido por palavras-chave no
texto do agente (bloco `text`). Heurística grosseira, documentada aqui para
revisão; não é semântica real.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ----------------------------------------------------------------- parsing

_TARGET_FIELDS = ("file_path", "notebook_path", "path")

_HELP_PATTERNS = re.compile(
    r"preciso de ajuda|não consigo|nao consigo|estou travado|"
    r"poderia (me )?(ajudar|esclarecer|confirmar)|"
    r"could you (please )?(clarify|confirm|help)|"
    r"i (need|require) (help|clarification|guidance)|"
    r"i'?m (stuck|unable|not sure how)|"
    r"unable to (proceed|continue) without|"
    r"unclear (how|what) to proceed",
    re.IGNORECASE,
)

RECOVERY_WINDOW_TURNS = 3
THRASH_THRESHOLD = 3


def _load_events(trace_path: str | Path) -> list[dict]:
    events = []
    for line in Path(trace_path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue  # linha não-JSON (ruído) — ignora, igual ao design 1
    return events


def _target(tool_name: str, tool_input: dict) -> str:
    for field in _TARGET_FIELDS:
        if field in tool_input:
            return str(tool_input[field])
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        return cmd.split()[0] if cmd else ""
    return json.dumps(tool_input, sort_keys=True)


def _signature(tool_name: str, tool_input: dict) -> str:
    return f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(parts)
    return str(content or "")


def _stop_reason(result_event: dict | None) -> str:
    if result_event is None:
        return "incomplete"
    if not result_event.get("is_error"):
        return "success"
    subtype = str(result_event.get("subtype") or "")
    if "max_turns" in subtype:
        return "max_turns"
    if "timeout" in subtype.lower():
        return "timeout"
    return "error"


def parse_trace(trace_path: str | Path) -> dict:
    """Lê `trace_path` e devolve as métricas de processo do SPEC-J2:
    n_turns, n_tool_calls, n_tool_errors, n_recovered, n_thrash,
    n_help_requests, stop_reason. Chave extra `recovered_pairs` (linhas
    erro→correção, para citação de P3) não é contrato da spec, é bônus."""
    events = _load_events(trace_path)

    turn_order: list[str] = []  # ordem de aparição dos message.id
    calls: list[dict] = []  # 1 entrada por tool_use, na ordem
    calls_by_id: dict[str, dict] = {}
    result_event: dict | None = None
    n_help_requests = 0

    for lineno, ev in enumerate(events, start=1):
        etype = ev.get("type")
        if etype == "assistant":
            msg = ev.get("message", {}) or {}
            mid = msg.get("id") or f"_noid_{lineno}"
            if mid not in turn_order:
                turn_order.append(mid)
            turn = len(turn_order)  # 1-based, turno da mensagem atual
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input") or {}
                    entry = {
                        "tool_use_id": block.get("id"),
                        "tool_name": tool_name,
                        "input": tool_input,
                        "target": _target(tool_name, tool_input),
                        "signature": _signature(tool_name, tool_input),
                        "turn": turn,
                        "line": lineno,
                        "is_error": None,
                        "error_text": "",
                        "result_line": None,
                    }
                    calls.append(entry)
                    if entry["tool_use_id"]:
                        calls_by_id[entry["tool_use_id"]] = entry
                elif btype == "text" and _HELP_PATTERNS.search(block.get("text", "") or ""):
                    n_help_requests += 1
        elif etype == "user":
            msg = ev.get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        entry = calls_by_id.get(block.get("tool_use_id"))
                        if entry is None:
                            continue
                        entry["is_error"] = bool(block.get("is_error"))
                        entry["result_line"] = lineno
                        if entry["is_error"]:
                            entry["error_text"] = _result_text(block.get("content"))
        elif etype == "result":
            result_event = ev  # design 1: única na trilha, guarda a última

    n_tool_calls = len(calls)
    errors = [c for c in calls if c["is_error"] is True]
    n_tool_errors = len(errors)

    # recuperação: erro em `target`, depois chamada DIFERENTE (assinatura
    # diferente) no MESMO alvo, dentro de RECOVERY_WINDOW_TURNS turnos,
    # que teve sucesso.
    n_recovered = 0
    recovered_pairs = []
    for err in errors:
        for cand in calls:
            if cand is err or not cand["target"] or cand["target"] != err["target"]:
                continue
            if cand["signature"] == err["signature"]:
                continue
            if not (err["turn"] < cand["turn"] <= err["turn"] + RECOVERY_WINDOW_TURNS):
                continue
            if cand["is_error"] is True:
                continue
            if cand["is_error"] is False or cand["is_error"] is None and cand["result_line"] is not None:
                n_recovered += 1
                recovered_pairs.append((err["line"], cand["result_line"]))
                break

    # thrash: mesma assinatura + mesmo texto de erro, >= THRASH_THRESHOLD vezes.
    thrash_groups: dict[tuple[str, str], int] = {}
    for err in errors:
        key = (err["signature"], err["error_text"])
        thrash_groups[key] = thrash_groups.get(key, 0) + 1
    n_thrash = sum(1 for count in thrash_groups.values() if count >= THRASH_THRESHOLD)

    n_turns = int(result_event.get("num_turns")) if result_event and result_event.get("num_turns") is not None else len(turn_order)
    stop_reason = _stop_reason(result_event)

    return {
        "n_turns": n_turns,
        "n_tool_calls": n_tool_calls,
        "n_tool_errors": n_tool_errors,
        "n_recovered": n_recovered,
        "n_thrash": n_thrash,
        "n_help_requests": n_help_requests,
        "stop_reason": stop_reason,
        "recovered_pairs": recovered_pairs,
    }


# --------------------------------------------------------------- X1/X2/X3

DISCARDED = "discarded"  # sentinela: critério não se aplica, não conta pra soma


def X1(metrics: dict) -> float:
    """Autonomia (peso 10). 10 se zero pedidos de ajuda e stop_reason ==
    'success'; -4 por pedido de ajuda; 0 se travou em max_turns/timeout."""
    if metrics["stop_reason"] in ("max_turns", "timeout"):
        return 0.0
    score = 10 - 4 * metrics["n_help_requests"]
    return float(max(0, min(10, score)))


def X2(metrics: dict):
    """Recuperação (peso 10). n_tool_errors == 0 -> DISCARDED (não infla nem
    pune). Caso contrário 10 * n_recovered / n_tool_errors, -3 se thrash,
    piso 0, arredondado (round-half-to-even do Python é irrelevante aqui —
    os aceites da spec caem em .5 exato só por coincidência de thrash)."""
    if metrics["n_tool_errors"] == 0:
        return DISCARDED
    score = 10 * metrics["n_recovered"] / metrics["n_tool_errors"]
    if metrics["n_thrash"] > 0:
        score -= 3
    return float(max(0, round(score)))


def X3(turns: int, cost: float, baseline_median: dict | None) -> float:
    """Fricção (peso 5) — reusa a fórmula do D4 (custo/turnos vs. mediana do
    baseline do build_id), só que na escala 0-5 em vez de 0-10.
    `baseline_median` = {"cost_usd": float, "turns": float} ou None (sem
    baseline ainda -> 5 por default, critério vira comparativo só a partir
    da 2a rodada, igual D4)."""
    if not baseline_median:
        return 5.0
    med_cost = baseline_median.get("cost_usd") or 0.0
    med_turns = baseline_median.get("turns") or 0.0
    if med_cost <= 0 and med_turns <= 0:
        return 5.0

    def _ratio_score(value: float, median: float) -> float:
        if median <= 0:
            return 5.0
        ratio = value / median
        if ratio <= 1:
            return 5.0
        if ratio >= 2:
            return 0.0
        return 5.0 * (2 - ratio)

    cost_score = _ratio_score(cost, med_cost)
    turns_score = _ratio_score(turns, med_turns)
    return float(min(cost_score, turns_score))
