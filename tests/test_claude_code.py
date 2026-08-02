import json
import subprocess
from pathlib import Path

import pytest

from harness.backends import claude_code as cc
from harness.backends import registry
from harness.backends.auth import AuthAdapter, NullAuth, available, get_auth
from harness.backends.base import Backend
from harness.types import ExecRequest

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code_result.json"
# Capturado de verdade em 2026-08-02 com `claude 2.1.220`, rodando
# `claude -p --output-format json --model haiku --permission-mode acceptEdits
#  --safe-mode --tools "Read,Edit"` num tmpdir com um target.py quebrado.
REAL_STDOUT = FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def no_cli(monkeypatch):
    monkeypatch.setattr(cc, "_which", lambda: None)


def _req(tmp_path: Path, **kw) -> ExecRequest:
    return ExecRequest(
        prompt="conserte o add",
        workspace=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
        **kw,
    )


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _stub_cli(monkeypatch, run):
    """CLI de mentira. O `--version` do preflight passa direto; o resto vai pro `run`."""
    monkeypatch.setattr(cc, "_which", lambda: "/usr/local/bin/claude")

    def dispatch(argv, **kw):
        if "--version" in argv:
            return _Proc("2.1.220 (Claude Code)\n")
        return run(argv, **kw)

    monkeypatch.setattr(cc.subprocess, "run", dispatch)


def _record(box: dict, stdout: str = REAL_STDOUT, returncode: int = 0):
    def run(argv, **kw):
        box["argv"], box["kw"] = argv, kw
        return _Proc(stdout, returncode)

    return run


# --- preflight -------------------------------------------------------------


def test_preflight_without_cli_is_not_ok(no_cli):
    pre = cc.ClaudeCodeBackend().preflight()
    assert pre.ok is False
    assert pre.reason == cc.MISSING_CLI


def test_preflight_reports_version(monkeypatch):
    monkeypatch.setattr(cc, "_which", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **kw: _Proc("2.1.220 (Claude Code)\n"))
    pre = cc.ClaudeCodeBackend().preflight()
    assert (pre.ok, pre.reason) == (True, "2.1.220 (Claude Code)")


def test_preflight_nonzero_version_is_not_ok(monkeypatch):
    monkeypatch.setattr(cc, "_which", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **kw: _Proc("", 127, "boom"))
    pre = cc.ClaudeCodeBackend().preflight()
    assert pre.ok is False and "boom" in pre.reason


def test_execute_without_cli_is_blocked(no_cli, tmp_path):
    res = cc.ClaudeCodeBackend().execute(_req(tmp_path))
    assert (res.ok, res.exit_reason, res.files_changed) == (False, "blocked", ())
    assert json.loads(res.trace_path.read_text())["error"] == cc.MISSING_CLI


# --- parse do JSON real ----------------------------------------------------


def test_parse_real_result():
    raw = cc._parse(REAL_STDOUT)
    assert raw["session_id"] == "5f3abc1b-71eb-45ce-a030-da7deb3ef556"
    assert cc._exit_reason(raw, 0) == "done"
    assert cc._cost(raw) == pytest.approx(0.0158347)
    # entrada = inputTokens + cache lido + cache criado (disjuntos na API).
    assert cc._tokens(raw) == (579 + 10697 + 5808, 514)


def test_parse_ignores_noise_before_the_json():
    raw = cc._parse("warning: algo\n" + REAL_STDOUT)
    assert raw["num_turns"] == 3


def test_parse_of_garbage_is_error():
    assert cc._parse("nada de json") == {}
    assert cc._exit_reason({}, 0) == "error"


def test_exit_reason_api_error():
    # Medido: erro de API vem com subtype "success" e is_error true.
    raw = {"is_error": True, "subtype": "success", "permission_denials": []}
    assert cc._exit_reason(raw, 1) == "error"


def test_exit_reason_permission_denial_is_blocked():
    raw = {"is_error": True, "subtype": "success", "permission_denials": [{"tool_name": "Edit"}]}
    assert cc._exit_reason(raw, 1) == "blocked"


def test_exit_reason_max_turns():
    assert cc._exit_reason({"subtype": "error_max_turns", "is_error": True}, 1) == "max_turns"


def test_execute_parses_result_and_diffs_files(monkeypatch, tmp_path):
    box: dict = {}
    target = tmp_path / "target.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    def run(argv, **kw):
        box["argv"], box["kw"] = argv, kw
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return _Proc(REAL_STDOUT)

    _stub_cli(monkeypatch, run)
    res = cc.ClaudeCodeBackend().execute(_req(tmp_path))

    assert (res.ok, res.exit_reason, res.turns) == (True, "done", 3)
    assert res.cost_usd == pytest.approx(0.0158347)
    assert res.files_changed == ("target.py",)
    assert res.session_id == "5f3abc1b-71eb-45ce-a030-da7deb3ef556"
    assert json.loads(res.trace_path.read_text())["type"] == "result"


# --- linha de comando ------------------------------------------------------


def test_argv_uses_real_flags(tmp_path):
    argv = cc._argv(_req(tmp_path, model="haiku"), model="haiku", exe="claude")
    assert argv[:1] == ["claude"]
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--safe-mode" in argv
    # `--max-turns` não existe no CLI 2.1.220; nada de flag inventada.
    assert not any(a.startswith("--max-turn") for a in argv)


def test_argv_resume_and_tools(tmp_path):
    req = _req(tmp_path, session_id="abc-123", tools=("Read", "Edit"))
    argv = cc._argv(req, model=None)
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert argv[argv.index("--tools") + 1] == "Read,Edit"
    assert "--model" not in argv


def test_prompt_goes_through_stdin_and_env_overrides(monkeypatch, tmp_path):
    box: dict = {}
    _stub_cli(monkeypatch, _record(box))
    monkeypatch.setenv("HARNESS_MARCADOR", "do-ambiente")
    cc.ClaudeCodeBackend().execute(_req(tmp_path, env={"HARNESS_MARCADOR": "do-req"}))

    assert box["kw"]["input"] == "conserte o add"
    assert box["kw"]["cwd"] == tmp_path
    assert box["kw"]["env"]["HARNESS_MARCADOR"] == "do-req"  # req.env por cima
    assert "PATH" in box["kw"]["env"]  # herda o os.environ


def test_model_do_atributo_quando_o_request_nao_traz(monkeypatch, tmp_path):
    box: dict = {}
    _stub_cli(monkeypatch, _record(box))
    cc.ClaudeCodeBackend(model="haiku").execute(_req(tmp_path))
    assert box["argv"][box["argv"].index("--model") + 1] == "haiku"


# --- timeout ---------------------------------------------------------------


def test_timeout_kills_and_reports(monkeypatch, tmp_path):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output="parcial")

    _stub_cli(monkeypatch, boom)
    res = cc.ClaudeCodeBackend().execute(_req(tmp_path, timeout_s=0.5))

    assert (res.ok, res.exit_reason, res.turns) == (False, "timeout", 0)
    assert res.cost_usd is None
    assert "timeout" in json.loads(res.trace_path.read_text().splitlines()[0])["error"]


def test_os_error_is_error(monkeypatch, tmp_path):
    def boom(argv, **kw):
        raise OSError("exec format error")

    _stub_cli(monkeypatch, boom)
    res = cc.ClaudeCodeBackend().execute(_req(tmp_path))
    assert (res.ok, res.exit_reason) == (False, "error")


# --- registro --------------------------------------------------------------


def test_backend_is_registered():
    assert "claude_code" in registry.available()
    backend = registry.get_backend("claude_code")
    assert isinstance(backend, Backend)
    assert backend.name == "claude_code"


def test_capabilities_are_declared():
    caps = cc.ClaudeCodeBackend().capabilities()
    assert (caps.resumable, caps.reports_cost, caps.model_selectable) == (True, True, True)
    assert {"Read", "Edit", "Bash"} <= caps.tools


# --- slot de auth ----------------------------------------------------------


def test_null_auth_is_the_default():
    assert available() == ["null"]
    auth = get_auth()
    assert isinstance(auth, NullAuth) and isinstance(auth, AuthAdapter)
    assert auth.env() == {}
    assert auth.check().ok is True


def test_unknown_auth_raises():
    with pytest.raises(KeyError):
        get_auth("oauth-de-assinatura")


def test_manual_auth_registration(monkeypatch):
    from harness.backends import auth as auth_registry

    auth_registry.register("fake", NullAuth)
    try:
        assert "fake" in auth_registry.available()
        assert isinstance(auth_registry.get_auth("fake"), NullAuth)
    finally:
        auth_registry.unregister("fake")
    assert "fake" not in auth_registry.available()


# --- execução real (gasta dinheiro) ----------------------------------------


@pytest.mark.claude_cli
def test_real_run_fixes_the_target(tmp_path, capsys):
    backend = cc.ClaudeCodeBackend()
    pre = backend.preflight()
    if not pre.ok:
        pytest.skip(pre.reason)

    target = tmp_path / "target.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    res = backend.execute(
        ExecRequest(
            prompt=(
                "O arquivo target.py tem add(a, b) que subtrai em vez de somar. "
                "Corrija para somar. Não faça mais nada."
            ),
            workspace=tmp_path,
            tools=("Read", "Edit"),
            model="haiku",
            timeout_s=180.0,
            trace_path=tmp_path / "trace.jsonl",
        )
    )
    with capsys.disabled():
        print(f"\nclaude_cli: {res.exit_reason} turns={res.turns} custo=${res.cost_usd}")

    assert res.ok is True and res.exit_reason == "done"
    assert "return a + b" in target.read_text(encoding="utf-8")
    assert "target.py" in res.files_changed
    assert res.cost_usd is not None and res.cost_usd > 0
    assert res.session_id
