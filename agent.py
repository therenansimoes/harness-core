#!/usr/bin/env python3
"""agent.py — harness v0.

Um agent mínimo: system prompt + bash + limite de turns, rodando dentro de um
workspace isolado. Dois backends:

  cli  (default) -> subprocess `claude -p`  ... custo = assinatura, budget baixo
  api            -> anthropic SDK           ... custo = tokens, para A/B sério

O harness INTEIRO editável vive neste arquivo. Evolução mexe aqui, nada mais.
Qualquer mudança em SYSTEM_PROMPT / MAX_TURNS / TOOLS é uma candidata a A/B.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- harness knobs
# Tudo abaixo desta linha é o "genoma" do harness. Um A/B muda UMA coisa aqui.

SYSTEM_PROMPT = """Você executa uma tarefa em um workspace isolado.

- Trabalhe apenas no diretório atual.
- Produza ARTEFATOS reais. Dizer que fez não conta.
- Não pergunte nem peça confirmação: execute.
- Termine com: DONE: <resumo em uma frase>.
"""

MAX_TURNS = 12
ALLOWED_TOOLS = ["Bash", "Read", "Write", "Edit"]
MODEL = os.environ.get("HARNESS_MODEL", "claude-haiku-4-5-20251001")
BACKEND = os.environ.get("HARNESS_BACKEND", "cli")
TIMEOUT_S = int(os.environ.get("HARNESS_TIMEOUT", "600"))

# ------------------------------------------------------------------- resultado


@dataclass
class AgentResult:
    ok: bool
    seconds: float
    tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    text: str = ""
    notes: str = ""
    raw: dict = field(default_factory=dict)


# ------------------------------------------------------------------- backends


def _run_cli(prompt: str, workspace: Path) -> AgentResult:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        MODEL,
        "--max-turns",
        str(MAX_TURNS),
        "--allowed-tools",
        *ALLOWED_TOOLS,
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        SYSTEM_PROMPT,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        )
    except subprocess.TimeoutExpired:
        return AgentResult(False, time.time() - t0, notes="timeout")
    elapsed = time.time() - t0

    if proc.returncode != 0:
        return AgentResult(
            False, elapsed, notes=f"cli_exit_{proc.returncode}:{proc.stderr[-200:].strip()}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return AgentResult(False, elapsed, text=proc.stdout[-500:], notes="bad_json")

    usage = data.get("usage") or {}
    tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    return AgentResult(
        ok=not data.get("is_error", False),
        seconds=elapsed,
        tokens=tokens,
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        turns=int(data.get("num_turns") or 0),
        text=str(data.get("result", ""))[-2000:],
        notes=data.get("subtype", "") if data.get("is_error") else "",
        raw=data,
    )


def _run_api(prompt: str, workspace: Path) -> AgentResult:
    """Backend API: loop bash manual. Só use quando quiser medir tokens crus."""
    import anthropic  # import tardio: o backend cli não precisa do SDK

    client = anthropic.Anthropic()
    bash_tool = {
        "name": "bash",
        "description": "Executa um comando shell no workspace e retorna stdout+stderr.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
    messages = [{"role": "user", "content": prompt}]
    t0, tokens, turns = time.time(), 0, 0

    while turns < MAX_TURNS:
        turns += 1
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            tools=[bash_tool],
            messages=messages,
        )
        tokens += resp.usage.input_tokens + resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return AgentResult(True, time.time() - t0, tokens, 0.0, turns, text[-2000:])

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                out = subprocess.run(
                    block.input["command"],
                    shell=True,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                payload = (out.stdout + out.stderr)[-8000:] or "(sem saída)"
            except subprocess.TimeoutExpired:
                payload = "(timeout de 120s)"
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": payload}
            )
        messages.append({"role": "user", "content": results})

    return AgentResult(False, time.time() - t0, tokens, 0.0, turns, notes="max_turns")


def run_agent(prompt: str, workspace: Path) -> AgentResult:
    if BACKEND == "api":
        return _run_api(prompt, workspace)
    if BACKEND == "cli":
        return _run_cli(prompt, workspace)
    raise SystemExit(f"HARNESS_BACKEND desconhecido: {BACKEND}")


if __name__ == "__main__":
    import sys

    ws = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path.cwd()
    r = run_agent(sys.argv[1], ws)
    print(json.dumps(r.__dict__ | {"raw": {}}, indent=2, ensure_ascii=False))
