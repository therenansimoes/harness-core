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
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import profile as profile_mod
import safety

ROOT = Path(__file__).parent.resolve()

# ---------------------------------------------------------------- harness knobs
# Tudo abaixo desta linha é o "genoma" do harness. Um A/B muda UMA coisa aqui.

SYSTEM_PROMPT = """Você executa uma tarefa em um workspace isolado.

- Trabalhe apenas no diretório atual.
- Produza ARTEFATOS reais. Dizer que fez não conta.
- Não pergunte nem peça confirmação: execute.
- Termine com: DONE: <resumo em uma frase>.
"""
# --- autopilot:prompt_tail ---

MAX_TURNS = 30
ALLOWED_TOOLS = ["Bash", "Read", "Write", "Edit"]
MODEL = os.environ.get("HARNESS_MODEL", "claude-haiku-4-5-20251001")
BACKEND = os.environ.get("HARNESS_BACKEND", "cli")
TIMEOUT_S = int(os.environ.get("HARNESS_TIMEOUT", "600"))
# trace.jsonl (SPEC-J2 design 1): teto de linhas do arquivo gravado por run e
# teto de tamanho por campo string dentro de cada evento — os dois elegíveis
# a A/B como o resto do genoma acima.
TRACE_MAX_LINES = 400
TRACE_MAX_FIELD = 2000

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
    trace_path: str = ""
    trace_lines: int = 0


# --------------------------------------------------------------------- trace


def _parse_stream(stdout: str) -> tuple[dict | None, list[str]]:
    """`--output-format stream-json` emite 1 evento JSON por linha. Ignora
    linha não-JSON; guarda a última `type=="result"` (mesmo objeto que
    `--output-format json` devolvia sozinho antes). `lines` preserva a ordem
    original — é o que vira trace.jsonl (nº da linha = chave de citação)."""
    result = None
    lines = []
    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            evt = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        lines.append(raw_line)
        if isinstance(evt, dict) and evt.get("type") == "result":
            result = evt
    return result, lines


def _truncate_fields(obj, max_field: int):
    """Trunca recursivamente qualquer string de evento > max_field chars:
    prefixo + marcador determinístico com a contagem do que sobrou de fora."""
    if isinstance(obj, str):
        if len(obj) > max_field:
            return obj[:max_field] + f"…[trunc {len(obj) - max_field} chars]"
        return obj
    if isinstance(obj, dict):
        return {k: _truncate_fields(v, max_field) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_fields(v, max_field) for v in obj]
    return obj


def _relpath_or_abs(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _write_trace(lines: list[str], run_id: str) -> tuple[str, int]:
    """Grava runs/<run_id>/trace.jsonl na ordem original. Trunca campo>
    TRACE_MAX_FIELD por evento; arquivo >TRACE_MAX_LINES mantém as 100
    primeiras + 299 últimas + 1 linha `harness_trunc` na posição 101 (total
    sempre TRACE_MAX_LINES). Teto duro de 2MB por cima de tudo. Devolve
    (path relativo ao repo quando possível, nº de linhas gravadas)."""
    trace_root = Path(os.environ.get("HARNESS_TRACE_ROOT", str(ROOT / "runs")))
    out_dir = trace_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "trace.jsonl"

    truncated = []
    for raw in lines:
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            truncated.append(raw)
            continue
        truncated.append(json.dumps(_truncate_fields(evt, TRACE_MAX_FIELD), ensure_ascii=False))

    if len(truncated) > TRACE_MAX_LINES:
        tail_n = TRACE_MAX_LINES - 100 - 1
        head, tail = truncated[:100], truncated[-tail_n:]
        dropped = len(truncated) - 100 - tail_n
        marker = json.dumps({"type": "harness_trunc", "dropped": dropped})
        truncated = head + [marker] + tail

    content = ("\n".join(truncated) + "\n") if truncated else ""
    encoded = content.encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        encoded = encoded[: 2 * 1024 * 1024]
        cut = encoded.rfind(b"\n")
        encoded = encoded[: cut + 1] if cut > 0 else b""
    out_path.write_bytes(encoded)
    return _relpath_or_abs(out_path), encoded.count(b"\n")


# ------------------------------------------------------------------- backends


def _system_prompt(workspace: Path) -> str:
    """SYSTEM_PROMPT + o que profile.py conseguiu detectar do projeto-alvo
    (D3, self-adaptive). Detecção pura, sem gravar nada no workspace: um
    .harness/profile.toml lá dentro sujaria o diff da run."""
    try:
        block = profile_mod.prompt_block(profile_mod.detect(workspace))
    except Exception:
        block = ""
    return SYSTEM_PROMPT + block


def _run_cli(prompt: str, workspace: Path) -> AgentResult:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        MODEL,
        "--max-turns",
        str(MAX_TURNS),
        "--allowed-tools",
        *ALLOWED_TOOLS,
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        _system_prompt(workspace),
    ]
    t0 = time.time()
    returncode, stdout, stderr = safety.safe_run(
        cmd,
        cwd=str(workspace),
        timeout=TIMEOUT_S,
        env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    )
    elapsed = time.time() - t0
    if stderr == "TIMEOUT" and returncode == -1:
        return AgentResult(False, elapsed, notes="timeout")

    data, lines = _parse_stream(stdout)
    trace_path, trace_lines = "", 0
    if lines:
        run_id = os.environ.get("HARNESS_RUN_ID") or workspace.name
        trace_path, trace_lines = _write_trace(lines, run_id)

    # `claude -p` sai com exit != 0 sempre que is_error=true no evento
    # result — inclusive em desfechos NORMAIS do harness como estourar
    # --max-turns (subtype=error_max_turns), que ainda assim carregam
    # usage/cost/turns reais. Tratar todo returncode!=0 como falha de infra
    # (como fazia antes) descarta esses números e reporta 0 tokens/$0 pra
    # uma run que trabalhou de verdade. Por isso: tenta achar o evento
    # result primeiro, independente do returncode; só cai em "cli_exit_N"
    # (infra quebrada de fato, sem trabalho do agente) quando não sobrou
    # nenhum evento result pra usar.
    if data is None:
        if returncode != 0:
            return AgentResult(
                False,
                elapsed,
                notes=f"cli_exit_{returncode}:{stderr[-200:].strip()}",
                trace_path=trace_path,
                trace_lines=trace_lines,
            )
        return AgentResult(
            False,
            elapsed,
            text=stdout[-500:],
            notes="bad_json",
            trace_path=trace_path,
            trace_lines=trace_lines,
        )

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
        trace_path=trace_path,
        trace_lines=trace_lines,
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
            system=_system_prompt(workspace),
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
                argv = shlex.split(block.input["command"])
                rc, out_s, err_s = safety.safe_run(argv, cwd=str(workspace), timeout=120)
                if err_s == "TIMEOUT" and rc == -1:
                    payload = "(timeout de 120s)"
                else:
                    payload = (out_s + err_s)[-8000:] or "(sem saída)"
            except safety.SafetyViolation as e:
                payload = f"(comando bloqueado pela allowlist: {e})"
            except ValueError as e:
                payload = f"(comando inválido: {e})"
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": payload}
            )
        messages.append({"role": "user", "content": results})

    return AgentResult(False, time.time() - t0, tokens, 0.0, turns, notes="max_turns")


def run_agent(prompt: str, workspace: Path) -> AgentResult:
    if os.environ.get("HARNESS_MOCK_AGENT") == "1":
        import mockagent

        return mockagent.run(prompt, workspace)
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
