"""Sandbox de SO (harness/backends/sandbox.py) — sem deepagents.

Testa config, geração de profile SBPL, embrulho de comando e fail-open do
factory; o último teste é smoke REAL contra o sandbox-exec do macOS.
"""

import logging
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from harness.backends import sandbox as sb


def test_load_settings_arquivo_ausente_fica_off(tmp_path):
    assert sb.load_settings(tmp_path / "nope.toml") == sb.SandboxSettings()


def test_load_settings_le_secao_executor(tmp_path):
    cfg = tmp_path / "tools.toml"
    cfg.write_text(
        '[executor]\nsandbox = "workspace-write"\nsandbox_network = "localhost"\n'
        'sandbox_extra_write = ["/opt/cache"]\n',
        encoding="utf-8",
    )
    settings = sb.load_settings(cfg)
    assert settings.mode == sb.MODE_WORKSPACE_WRITE
    assert settings.network == sb.NET_LOCALHOST
    assert settings.extra_write == ("/opt/cache",)


def test_load_settings_valor_invalido_fail_open(tmp_path, caplog):
    cfg = tmp_path / "tools.toml"
    cfg.write_text(
        '[executor]\nsandbox = "banana"\nsandbox_network = "wifi"\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="harness.sandbox"):
        settings = sb.load_settings(cfg)
    assert settings.mode == sb.MODE_OFF
    assert settings.network == sb.NET_DENY
    assert caplog.records


def test_load_settings_toml_quebrado_fail_open(tmp_path):
    cfg = tmp_path / "tools.toml"
    cfg.write_text("not [ toml", encoding="utf-8")
    assert sb.load_settings(cfg) == sb.SandboxSettings()


def test_generate_profile_roots_e_dev(tmp_path):
    p = sb.generate_profile([tmp_path], sb.NET_DENY)
    assert "(allow default)" in p
    assert "(deny file-write*)" in p
    assert f'(subpath "{tmp_path.resolve()}")' in p
    assert '(literal "/dev/null")' in p
    assert "(deny network*)" in p


def test_generate_profile_modos_de_rede(tmp_path):
    aberto = sb.generate_profile([tmp_path], sb.NET_ALLOW)
    assert "(deny network*)" not in aberto
    local = sb.generate_profile([tmp_path], sb.NET_LOCALHOST)
    assert "(deny network*)" in local
    assert 'remote ip "localhost:*"' in local


def test_generate_profile_recusa_aspas_no_path(tmp_path):
    with pytest.raises(ValueError):
        sb.generate_profile([Path(str(tmp_path) + '/a"b')], sb.NET_DENY)


def test_wrap_preserva_o_comando_original():
    s = sb.DarwinSeatbeltSandbox(profile_path=Path("/x y/p.sb"))
    w = s.wrap("echo 'a b'")
    assert w.startswith("/usr/bin/sandbox-exec -f ")
    assert shlex.quote("/x y/p.sb") in w
    # round-trip: o comando original sobrevive a uma camada de shell
    assert shlex.split(w)[-1] == "echo 'a b'"


def test_make_sandbox_off_retorna_none(tmp_path):
    assert sb.make_sandbox(tmp_path, sb.SandboxSettings()) is None


def test_make_sandbox_plataforma_nao_darwin_fail_open(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="harness.sandbox"):
        result = sb.make_sandbox(
            tmp_path, sb.SandboxSettings(mode=sb.MODE_WORKSPACE_WRITE), platform="linux"
        )
    assert result is None
    assert caplog.records


def test_make_sandbox_binario_ausente_fail_open(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(sb, "SANDBOX_EXEC", str(tmp_path / "absent"))
    monkeypatch.setattr(sb.shutil, "which", lambda _: None)
    with caplog.at_level(logging.WARNING, logger="harness.sandbox"):
        result = sb.make_sandbox(
            tmp_path, sb.SandboxSettings(mode=sb.MODE_WORKSPACE_WRITE), platform="darwin"
        )
    assert result is None
    assert caplog.records


def test_make_sandbox_erro_no_profile_fail_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sb, "SANDBOX_EXEC", sys.executable)  # existe
    monkeypatch.setattr(
        sb, "generate_profile", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with caplog.at_level(logging.WARNING, logger="harness.sandbox"):
        result = sb.make_sandbox(
            tmp_path, sb.SandboxSettings(mode=sb.MODE_WORKSPACE_WRITE), platform="darwin"
        )
    assert result is None
    assert caplog.records


def test_make_sandbox_escreve_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "SANDBOX_EXEC", sys.executable)  # existe
    result = sb.make_sandbox(
        tmp_path,
        sb.SandboxSettings(mode=sb.MODE_WORKSPACE_WRITE),
        platform="darwin",
        profile_dir=tmp_path / "pd",
    )
    assert isinstance(result, sb.DarwinSeatbeltSandbox)
    conteudo = (tmp_path / "pd" / "harness-sandbox.sb").read_text(encoding="utf-8")
    assert f'(subpath "{tmp_path.resolve()}")' in conteudo


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="sandbox-exec indisponível",
)
def test_seatbelt_nega_escrita_fora_do_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = sb.make_sandbox(
        ws,
        sb.SandboxSettings(mode=sb.MODE_WORKSPACE_WRITE, network=sb.NET_ALLOW),
        write_roots=[ws],
        profile_dir=tmp_path / "pd",
    )
    assert sbx is not None
    ok = subprocess.run(
        sbx.wrap("echo hi > inside.txt"),
        shell=True,
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0
    assert (ws / "inside.txt").read_text().strip() == "hi"
    target = tmp_path / "outside.txt"  # fora do único write_root
    denied = subprocess.run(
        sbx.wrap(f"echo hi > {target}"),
        shell=True,
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert denied.returncode != 0
    assert not target.exists()
    assert "not permitted" in (denied.stderr + denied.stdout).lower()
