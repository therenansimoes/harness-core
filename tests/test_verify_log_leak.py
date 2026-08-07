"""O log do verify não pode vazar o golden para a tentativa seguinte.

O verificador é selado (só existe no workspace durante o verify), mas o log que
ele imprime ficava no próprio workspace: no retry o agente leria ali a resposta
esperada e passaria a régua sem resolver nada. O log mora fora do ws.
"""

import subprocess
from pathlib import Path

import pytest

from harness import cli
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.ruler.verify import log_tail

SECRET = "SEGREDO_XYZ"
# Verificador falante: reprova E imprime o gabarito, que é o pior caso.
VERIFY_PY = f'print("esperado={SECRET}")\nraise SystemExit(1)\n'


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


def _unit(tmp_path: Path, name: str) -> Path:
    unit = tmp_path / name
    unit.mkdir()
    (unit / "unit.toml").write_text(
        f'id = "{name}"\nkind = "code"\nprompt = "x"\nverify_cmd = "python verify.py"\n',
        encoding="utf-8",
    )
    (unit / "verify.py").write_text(VERIFY_PY, encoding="utf-8")
    return unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "alvo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_o_golden_do_verify_nao_sobra_no_workspace(data_dir, tmp_path):
    unit = _unit(tmp_path, "falante")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-leak", max_attempts=2)
    ws = Path(final["workspace"])
    assert ws.is_dir(), "sem workspace o teste passaria por vacuidade"

    vazou = [
        p
        for p in ws.rglob("*")
        if p.is_file() and SECRET in p.read_bytes().decode("utf-8", errors="replace")
    ]
    assert vazou == [], f"gabarito legível no workspace: {vazou}"
    assert not (ws / "verify.log").exists()
    assert not (ws / ".harness" / "verify.log").exists()


def test_log_de_cada_tentativa_vive_no_data_dir(data_dir, tmp_path):
    unit = _unit(tmp_path, "falante")
    final = run_unit(unit, "mock", None, data_dir, thread_id="t-log", max_attempts=2)

    logs = data_dir / "logs" / "t-log"
    assert (logs / "verify.a0.log").is_file()
    assert (logs / "verify.a1.log").is_file()
    # Fora do ws o log sobrevive ao run: é isso que o diagnóstico depende.
    assert log_tail(final["verdict"].log_path) != ""

    saved = store.get_node("t-log", "verify", attempt=1)
    assert saved is not None
    assert SECRET in saved["tail"]


def test_run_once_tambem_grava_fora_do_repo(data_dir, tmp_path):
    repo = _repo(tmp_path)
    unit = cli.load_unit(_unit(tmp_path, "falante"))
    outcome = cli.run_once(unit, "mock", repo=str(repo))

    assert not (repo / ".harness" / "verify.log").exists()
    log = store.data_dir() / "logs" / outcome.row.run_id / "verify.log"
    assert log.is_file()
    assert outcome.verify_tail != ""


def test_run_once_salva_trace_em_data_logs(data_dir, tmp_path):
    """run_once deve copiar trace.jsonl para data/logs/<run_id>/trace.0.jsonl."""
    unit = cli.load_unit(_unit(tmp_path, "rastreavel"))
    outcome = cli.run_once(unit, "mock")

    trace = store.data_dir() / "logs" / outcome.row.run_id / "trace.0.jsonl"
    assert trace.is_file(), f"trace ausente em {trace}"


def test_run_once_trace_com_repo(data_dir, tmp_path):
    """run_once com --repo tambem deve salvar o trace."""
    repo = _repo(tmp_path)
    unit = cli.load_unit(_unit(tmp_path, "rastreavel2"))
    outcome = cli.run_once(unit, "mock", repo=str(repo))

    trace = store.data_dir() / "logs" / outcome.row.run_id / "trace.0.jsonl"
    assert trace.is_file(), f"trace ausente em {trace}"
