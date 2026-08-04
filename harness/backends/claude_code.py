"""Backend claude_code: subprocess do Claude Code CLI oficial.

A autenticação é a *nativa* do CLI (`claude auth login`, assinatura ou API key):
este módulo nunca lê, escreve nem repassa credencial — ele só monta a linha de
comando e escolhe o cwd. É por isso que ele não consulta o slot `harness.auth`
(risco 7 da SPEC).

Todas as flags usadas foram conferidas em `claude --help` (2.1.220), nada é
chute. O que NÃO existe nesta versão está anotado em `_argv`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

# `deepagents_backend` só importa stdlib no topo (LangChain é lazy lá dentro),
# então reusar o snapshot de mtime daqui não puxa o extra.
from harness.backends.deepagents_backend import _diff, _snapshot
from harness.types import Capabilities, ExecRequest, ExecResult, ExitReason, Preflight

CLI = "claude"
VERSION_TIMEOUT_S = 10.0

# `--permission-mode` do help: choices "acceptEdits", "auto", "bypassPermissions",
# "manual", "dontAsk", "plan". `acceptEdits` é o menos permissivo que ainda deixa
# o agente editar arquivo sem prompt interativo.
PERMISSION_MODE = "acceptEdits"

# Tools embutidas do CLI. `req.tools` é repassado VERBATIM para `--tools`, então
# quem restringe fala este vocabulário (não o do deepagents). Nome desconhecido
# é silenciosamente ignorado pelo CLI — allowlist errada = agente sem tool.
TOOLS = frozenset(
    {
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "Read",
        "Task",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

MISSING_CLI = "claude CLI não encontrado — instale e autentique (claude.ai/code)"


class ClaudeCodeBackend:
    name: ClassVar[str] = "claude_code"

    def __init__(self, model: str | None = None) -> None:
        # Mesmo contrato do deepagents: `preflight()` não recebe argumento, quem
        # sabe o modelo seta o atributo (o `cmd_run` do cli faz isso).
        self.model = model

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable=True,  # `-r, --resume [value]`
            reports_cost=True,  # `total_cost_usd` no objeto final do --output-format json
            model_selectable=True,  # `--model <model>`
            tools=TOOLS,
            streaming=False,  # existe `--output-format stream-json`; aqui não é usado
        )

    def preflight(self) -> Preflight:
        """Binário no PATH + `--version`. ZERO chamada de LLM."""
        exe = _which()
        if exe is None:
            return Preflight(ok=False, reason=MISSING_CLI)
        try:
            proc = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return Preflight(
                ok=False, reason=f"{CLI} --version falhou: {type(exc).__name__}: {exc}"
            )
        if proc.returncode != 0:
            detail = proc.stderr.strip()[:200]
            return Preflight(ok=False, reason=f"{CLI} --version saiu {proc.returncode}: {detail}")
        return Preflight(ok=True, reason=proc.stdout.strip() or exe)

    def execute(self, req: ExecRequest) -> ExecResult:
        pre = self.preflight()
        if not pre.ok:
            return _failure(req, "blocked", pre.reason)

        req.workspace.mkdir(parents=True, exist_ok=True)
        before = _snapshot(req.workspace, exclude=req.trace_path)
        argv = _argv(req, model=req.model or self.model, exe=_which() or CLI)
        try:
            proc = subprocess.run(
                argv,
                cwd=req.workspace,
                input=req.prompt,  # prompt por stdin: não estoura ARG_MAX
                capture_output=True,
                text=True,
                timeout=req.timeout_s,
                env=_env(req.env),
            )
        except subprocess.TimeoutExpired as exc:
            # `subprocess.run` já mata o filho antes de propagar.
            parcial, motivo = _decode(exc.stdout), f"timeout após {req.timeout_s}s"
            _write_trace(req.trace_path, parcial, error=motivo)
            return _result(req, before, raw={}, exit_reason="timeout")
        except OSError as exc:
            return _failure(req, "error", f"{type(exc).__name__}: {exc}")

        _write_trace(req.trace_path, proc.stdout, error=proc.stderr.strip()[:2000] or None)
        raw = _parse(proc.stdout)
        return _result(req, before, raw=raw, exit_reason=_exit_reason(raw, proc.returncode))


# --------------------------------------------------------------------------- processo


def _which() -> str | None:
    """Indireção só para o teste conseguir simular CLI ausente sem mexer no PATH."""
    return shutil.which(CLI)


def _env(overrides: Mapping[str, str]) -> dict[str, str]:
    """`req.env` por cima do ambiente do processo. Credencial não passa por aqui:
    quem autentica é o próprio CLI."""
    return {**os.environ, **overrides}


def _argv(req: ExecRequest, model: str | None, exe: str = CLI) -> list[str]:
    """Linha de comando headless.

    Não existe `--max-turns` no CLI 2.1.220: `req.max_turns` não tem para onde ir
    e quem segura run desgovernado é o `timeout_s`.

    `--safe-mode` desliga CLAUDE.md, hooks, skills, plugins e MCP do operador
    (auth, modelo, tools e permissões continuam normais). Sem ele o resultado de
    um run dependeria do `~/.claude` de quem rodou — o que envenenaria qualquer
    A/B de backend.
    """
    argv = [
        exe,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        PERMISSION_MODE,
        "--safe-mode",
    ]
    if model:
        argv += ["--model", model]
    if req.tools:
        argv += ["--tools", ",".join(req.tools)]
    if req.session_id:
        argv += ["--resume", req.session_id]
    return argv


def _decode(chunk: Any) -> str:
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", "replace")
    return chunk or ""


# --------------------------------------------------------------------------- parse


def _parse(stdout: str) -> dict[str, Any]:
    """`--output-format json` imprime UM objeto. Se vier ruído antes, vale a
    última linha que parseia; nada parseável => dict vazio (=> exit_reason erro)."""
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        obj: Any = json.loads(text)
    except json.JSONDecodeError:
        obj = None
        for line in reversed(text.splitlines()):
            try:
                obj = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return obj if isinstance(obj, dict) else {}


def _exit_reason(raw: dict[str, Any], returncode: int) -> ExitReason:
    """`subtype` mente (vem "success" até em erro de API), então `is_error` manda."""
    if not raw:
        return "error"
    if "max_turns" in str(raw.get("subtype") or ""):
        return "max_turns"
    if raw.get("is_error") or returncode != 0:
        # Denial registrado num run que falhou = a allowlist barrou o agente,
        # não o modelo errou. Distinção que o ledger precisa.
        return "blocked" if raw.get("permission_denials") else "error"
    return "done"


def _tokens(raw: dict[str, Any]) -> tuple[int | None, int | None]:
    """`modelUsage` é o agregado da sessão; o `usage` do topo é parcial (medido:
    26 vs 579 tokens de entrada no mesmo run). Cache conta como entrada — na API
    da Anthropic esses campos são disjuntos de `input_tokens`."""
    per_model = raw.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        rows = [v for v in per_model.values() if isinstance(v, dict)]
        cache = ("cacheReadInputTokens", "cacheCreationInputTokens")
        tin = sum(_int(v, "inputTokens") + sum(_int(v, k) for k in cache) for v in rows)
        return tin, sum(_int(v, "outputTokens") for v in rows)
    usage = raw.get("usage")
    if isinstance(usage, dict):
        tin = (
            _int(usage, "input_tokens")
            + _int(usage, "cache_read_input_tokens")
            + _int(usage, "cache_creation_input_tokens")
        )
        return tin, _int(usage, "output_tokens")
    return None, None


def _int(d: dict[str, Any], key: str) -> int:
    try:
        return int(d.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _cost(raw: dict[str, Any]) -> float | None:
    value = raw.get("total_cost_usd")
    if isinstance(value, (int, float)):
        return float(value)
    return None


# --------------------------------------------------------------------------- resultado


def _result(
    req: ExecRequest,
    before: dict[str, tuple[int, int]],
    *,
    raw: dict[str, Any],
    exit_reason: ExitReason,
) -> ExecResult:
    tin, tout = _tokens(raw)
    return ExecResult(
        ok=exit_reason == "done",
        exit_reason=exit_reason,
        turns=_int(raw, "num_turns"),
        cost_usd=_cost(raw),
        tokens_in=tin,
        tokens_out=tout,
        files_changed=_diff(before, _snapshot(req.workspace, exclude=req.trace_path)),
        session_id=raw.get("session_id") or req.session_id,
        trace_path=req.trace_path,
    )


def _failure(req: ExecRequest, exit_reason: ExitReason, reason: str) -> ExecResult:
    _write_trace(req.trace_path, "", error=reason)
    return ExecResult(
        ok=False,
        exit_reason=exit_reason,
        turns=0,
        cost_usd=None,
        tokens_in=None,
        tokens_out=None,
        files_changed=(),
        session_id=req.session_id,
        trace_path=req.trace_path,
    )


def _write_trace(path: Path, stdout: str, error: str | None = None) -> None:
    """O trace é o stdout JSON bruto do CLI, uma linha por objeto."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            if error:
                fh.write(json.dumps({"error": error}, ensure_ascii=False) + "\n")
            text = (stdout or "").strip()
            if text:
                fh.write(text + "\n")
    except OSError:  # trace é diagnóstico, não pode derrubar o run
        pass
