"""Fase de setup: cache por lockfile, stamp só no sucesso, flock por projeto."""

import os
import subprocess
import threading
from pathlib import Path

import pytest

from harness.graph.run_graph import run_unit
from harness.projects import Project, init_project
from harness.routing import CONFIG_DIR_ENV
from harness.workspace import setup


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def _ws(tmp_path: Path, name: str, lock: str = "left-pad==1\n") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    (ws / "requirements.txt").write_text(lock, encoding="utf-8")
    return ws


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def _toy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "toyrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _proj(name: str, cmd: str, timeout: int = 30) -> Project:
    return Project(name=name, repo=Path("/nao/usado"), setup_cmd=cmd, setup_timeout=timeout)


def test_hash_muda_com_lockfile(tmp_path):
    ws = _ws(tmp_path, "ws")
    antes = setup.lock_hash(ws, "npm ci")
    (ws / "requirements.txt").write_text("left-pad==2\n", encoding="utf-8")
    assert setup.lock_hash(ws, "npm ci") != antes
    # o comando entra no hash: trocar de gerenciador reinstala.
    assert setup.lock_hash(ws, "npm ci") != setup.lock_hash(ws, "npm install")
    # lockfile novo (ausente antes) também muda — o caso do `uv.lock` adicionado.
    depois = setup.lock_hash(ws, "npm ci")
    (ws / "uv.lock").write_text("x\n", encoding="utf-8")
    assert setup.lock_hash(ws, "npm ci") != depois


def test_segundo_ensure_skipa(tmp_path, data_dir):
    ws = _ws(tmp_path, "ws")
    proj = _proj("toy", "mkdir -p .venv && echo run >> ../ran.txt")

    first = setup.ensure(ws, proj)
    assert first == {"skipped": False, "sec": pytest.approx(first["sec"]), "ok": True}
    assert setup.stamp_path("toy").is_file()
    assert (ws / ".harness" / "setup.log").is_file()

    second = setup.ensure(ws, proj)
    assert second["skipped"] is True and second["ok"] is True
    assert (tmp_path / "ran.txt").read_text(encoding="utf-8").count("run") == 1

    # lockfile mexido invalida o cache: roda de novo.
    (ws / "requirements.txt").write_text("left-pad==2\n", encoding="utf-8")
    assert setup.ensure(ws, proj)["skipped"] is False
    assert (tmp_path / "ran.txt").read_text(encoding="utf-8").count("run") == 2


def test_marker_ausente_reinstala(tmp_path, data_dir):
    """Stamp válido mas `.venv` apagado na origem (o symlink do provision aponta
    para o repo real) não pode contar como cache quente."""
    ws = _ws(tmp_path, "ws")
    proj = _proj("toy", "mkdir -p .venv")
    setup.ensure(ws, proj)
    (ws / ".venv").rmdir()
    assert setup.ensure(ws, proj)["skipped"] is False


def test_rc_nao_zero_nao_grava_stamp(tmp_path, data_dir):
    ws = _ws(tmp_path, "ws")
    res = setup.ensure(ws, _proj("toy", "echo estourou >&2; exit 3"))
    assert res == {"skipped": False, "sec": pytest.approx(res["sec"]), "ok": False}
    assert not setup.stamp_path("toy").is_file()
    assert "estourou" in (ws / ".harness" / "setup.log").read_text(encoding="utf-8")


def test_sem_manifesto_nada_roda(tmp_path, data_dir):
    ws = tmp_path / "vazio"
    ws.mkdir()
    proj = Project(name="toy", repo=Path("/nao/usado"))
    assert setup.ensure(ws, proj) == {"skipped": True, "sec": 0.0, "ok": True}
    assert not setup.setup_dir().exists()


def test_deteccao_automatica(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    assert setup.detect_cmd(ws) is None
    (ws / "package.json").write_text("{}\n", encoding="utf-8")
    assert setup.detect_cmd(ws) == "npm install"
    (ws / "package-lock.json").write_text("{}\n", encoding="utf-8")
    assert setup.detect_cmd(ws) == "npm ci"
    assert setup.stack_marker(ws) == ws / "node_modules"


def test_ensures_concorrentes_serializam(tmp_path, data_dir):
    """Dois runs paralelos do MESMO projeto instalam no mesmo diretório (o
    provision symlinka o cache da origem). A sentinela falha se houver overlap.

    Lockfiles diferentes de propósito: com o mesmo hash o segundo skiparia e o
    teste não provaria nada sobre o lock.
    """
    sent = tmp_path / "sentinela"
    cmd = f"if [ -e {sent} ]; then exit 9; fi; touch {sent}; sleep 0.3; rm {sent}"
    proj = _proj("toy", cmd)
    was = [_ws(tmp_path, "ws_a", "a\n"), _ws(tmp_path, "ws_b", "b\n")]
    out: dict[int, dict] = {}

    def go(i: int) -> None:
        out[i] = setup.ensure(was[i], proj)

    threads = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(out) == 2, out
    assert all(r["ok"] for r in out.values()), out  # rc 9 = rodaram junto
    assert all(not r["skipped"] for r in out.values()), out
    assert not sent.exists()
    # Serializado: o segundo esperou o primeiro (0.3s cada).
    assert max(r["sec"] for r in out.values()) >= 0.3


def test_provision_de_projeto_grava_sec_setup(tmp_path, config_dir, data_dir):
    repo = _toy_repo(tmp_path)
    init_project(
        repo,
        "toy",
        queue_dir=tmp_path / "fila",
        setup_cmd="touch dep_instalada.txt",
        setup_timeout=30,
    )
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "s1"\nkind = "code"\nproject = "toy"\nprompt = "x"\n'
        'verify_cmd = "test -f dep_instalada.txt"\n',
        encoding="utf-8",
    )

    final = run_unit(unit, "mock", None, data_dir, thread_id="s-setup")
    ev = next(e for e in final["events"] if e["node"] == "provision")
    assert ev["sec_setup"] > 0
    assert ev["setup_skipped"] is False
    assert ev["setup_failed"] is False
    assert final["decision"].action == "accept"


# --- pin do python do projeto --------------------------------------------------


def _fake_uv(bin_dir: Path, monkeypatch, instaladas: str) -> Path:
    """`uv` de mentira no PATH: `python find <v>` só acha o que está em
    `instaladas`, `python pin <v>` grava o que foi pedido num arquivo de prova."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    prova = bin_dir / "pinned.txt"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'case "$1 $2" in\n'
        '  "python find")\n'
        f'    case " {instaladas} " in *" $3 "*) echo /fake/bin/python$3; exit 0;; esac\n'
        "    echo 'No interpreter found' >&2; exit 1;;\n"
        '  "python pin")\n'
        f'    echo "$3" > {prova}; echo "Pinned to $3"; exit 0;;\n'
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return prova


def test_pin_usa_a_versao_do_python_version(tmp_path, monkeypatch):
    ws = _ws(tmp_path, "ws")
    (ws / ".python-version").write_text("3.11.9\n", encoding="utf-8")
    prova = _fake_uv(tmp_path / "bin", monkeypatch, instaladas="3.11.9")

    ok, detalhe = setup.pin_python(ws)

    assert ok is True
    assert prova.read_text(encoding="utf-8").strip() == "3.11.9"
    assert "3.11.9" in detalhe


def test_versao_nao_instalada_falha_com_instrucao(tmp_path, data_dir, monkeypatch):
    ws = _ws(tmp_path, "ws")
    (ws / ".python-version").write_text("3.13.1\n", encoding="utf-8")
    prova = _fake_uv(tmp_path / "bin", monkeypatch, instaladas="3.11.9")

    res = setup.ensure(ws, _proj("toy", "touch NAO_DEVERIA_RODAR.txt"))

    assert res["ok"] is False
    assert not (ws / "NAO_DEVERIA_RODAR.txt").exists()  # nem chega no setup_cmd
    assert not prova.exists()  # e NÃO baixa toolchain
    log = (ws / ".harness" / "setup.log").read_text(encoding="utf-8")
    assert "uv python install 3.13.1" in log
    assert "NÃO baixa" in log


def test_sem_python_version_o_pin_e_noop(tmp_path, monkeypatch):
    ws = _ws(tmp_path, "ws")
    prova = _fake_uv(tmp_path / "bin", monkeypatch, instaladas="3.11.9")
    assert setup.pin_python(ws) == (True, "")
    assert not prova.exists()


def test_python_version_vazio_ou_comentado_e_ignorado(tmp_path, monkeypatch):
    ws = _ws(tmp_path, "ws")
    (ws / ".python-version").write_text("# nada aqui\n\n", encoding="utf-8")
    _fake_uv(tmp_path / "bin", monkeypatch, instaladas="3.11.9")
    ok, detalhe = setup.pin_python(ws)
    assert ok is True and "vazio" in detalhe


def test_uv_ausente_nao_derruba_o_setup(tmp_path, monkeypatch):
    ws = _ws(tmp_path, "ws")
    (ws / ".python-version").write_text("3.11.9\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "vazio"))  # sem uv no PATH
    ok, detalhe = setup.pin_python(ws)
    assert ok is True and "uv ausente" in detalhe


# --- redação e env do projeto no setup ----------------------------------------


def test_setup_log_redige_segredo(tmp_path, data_dir):
    ws = _ws(tmp_path, "ws")
    res = setup.ensure(ws, _proj("toy", "echo 'authToken=sk-vazando123456789'; exit 1"))
    assert res["ok"] is False
    log = (ws / ".harness" / "setup.log").read_text(encoding="utf-8")
    assert "sk-vazando123456789" not in log
    assert "***" in log


def test_ponteiro_do_env_e_gravado_e_o_setup_ve_a_var(tmp_path, data_dir):
    ws = _ws(tmp_path, "ws")
    envf = tmp_path / "projeto.env"
    envf.write_text("PRIVATE_INDEX=https://interno/simple\n", encoding="utf-8")
    proj = Project(
        name="toy",
        repo=tmp_path,
        setup_cmd='echo "idx=$PRIVATE_INDEX"',
        setup_timeout=30,
        env_file=str(envf),
    )

    res = setup.ensure(ws, proj)

    assert res["ok"] is True
    ptr = ws / ".harness" / "env_file"
    assert ptr.read_text(encoding="utf-8") == str(envf)
    log = (ws / ".harness" / "setup.log").read_text(encoding="utf-8")
    assert "idx=https://interno/simple" in log


def test_projeto_sem_env_file_nao_deixa_ponteiro(tmp_path, data_dir):
    ws = _ws(tmp_path, "ws")
    setup.ensure(ws, _proj("toy", "true"))
    assert not (ws / ".harness" / "env_file").exists()
