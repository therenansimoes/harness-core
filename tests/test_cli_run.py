import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from harness import cli
from harness.ledger import store
from harness.routing import CONFIG_DIR_ENV
from harness.ruler.verify import log_tail

REPO = Path(__file__).resolve().parent.parent
FIXTURE = str(Path(__file__).parent / "fixtures" / "echo")

# Tiers de teste apontando pro backend mock: `--route auto` com o models.toml do
# repo chamaria modelo de verdade, e teste não gasta hardware nem token.
MODELS_TOML = """\
[[tier]]
name = "t0"
backend = "mock"
model = ""
max_turns = 4
cost_rank = 0

[[tier]]
name = "t1"
backend = "mock"
model = ""
max_turns = 9
cost_rank = 1

[router]
default_tier = "t0"
max_attempts = 2
min_n = 6
prior_floor = 0.50

[router.kind]
code = "t0"
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    shutil.copy(REPO / "config" / "kinds.toml", cfg / "kinds.toml")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def test_run_mock_exits_zero_and_records_one_row(data_dir):
    rc = cli.main(["run", "--unit", FIXTURE, "--backend", "mock"])
    assert rc == 0

    db = data_dir / "runs.sqlite"
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    row = store.history()[0]
    assert row.unit_id == "echo"
    assert row.backend == "mock"
    assert row.kind == "code"
    assert row.ok is True
    assert row.exit_reason == "done"
    assert row.cost_usd == 0.0
    assert row.intervention is False


def test_run_records_failure_when_verify_fails(data_dir, tmp_path):
    unit = tmp_path / "bad"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "bad"\nprompt = "x"\nverify_cmd = "test -f nao_existe.txt"\n',
        encoding="utf-8",
    )
    assert cli.main(["run", "--unit", str(unit), "--backend", "mock"]) == 1
    assert store.history()[0].exit_reason == "verify_failed"


def test_verify_failure_shows_and_records_log_tail(data_dir, tmp_path, capsys):
    """`verify_failed:exit=N` sozinho não diz nada: o fim do log tem que sair no
    stderr e ficar no ledger, porque o workspace morre com o run."""
    unit = tmp_path / "loud"
    unit.mkdir()
    (unit / "unit.toml").write_text(
        'id = "loud"\nprompt = "x"\n'
        'verify_cmd = "echo BOOM_stdout; echo BOOM_stderr >&2; exit 3"\n',
        encoding="utf-8",
    )
    assert cli.main(["run", "--unit", str(unit), "--backend", "mock"]) == 1

    err = capsys.readouterr().err
    assert "BOOM_stdout" in err
    assert "BOOM_stderr" in err

    saved = store.get_node(store.history()[0].run_id, "verify")
    assert saved is not None
    assert "BOOM_stdout" in saved["tail"]
    assert saved["exit_reason"] == "verify_failed"


def test_log_tail_keeps_only_the_last_lines(tmp_path):
    log = tmp_path / "verify.log"
    log.write_text("".join(f"linha{i}\n" for i in range(40)), encoding="utf-8")
    tail = log_tail(log).splitlines()
    assert len(tail) == 15
    assert tail[0] == "linha25"
    assert tail[-1] == "linha39"


def test_unknown_backend_raises(data_dir):
    with pytest.raises(KeyError):
        cli.main(["run", "--unit", FIXTURE, "--backend", "nao-existe"])


def test_bootstrap_disables_tracing(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    cli._bootstrap()
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_backends_lists_mock(data_dir, capsys):
    assert cli.main(["backends"]) == 0
    out = capsys.readouterr().out
    assert "mock" in out
    assert "deepagents" in out


def test_model_flag_reaches_ledger_and_backend(data_dir, monkeypatch):
    from harness.backends import registry
    from harness.backends.mock import MockBackend

    class Spy(MockBackend):
        model = None

    spy = Spy()
    monkeypatch.setattr(registry, "get_backend", lambda name: spy)
    rc = cli.main(["run", "--unit", FIXTURE, "--backend", "mock", "--model", "openai:x-mlx"])

    assert rc == 0
    assert spy.model == "openai:x-mlx"
    assert store.history()[0].model == "openai:x-mlx"


def test_cli_route_auto_flag(data_dir, config_dir, capsys):
    """`--route auto` roda sem `--backend` (quem escolhe é o router) e recusa os
    dois juntos com erro de argparse — exit 2, não exceção no meio do run."""
    assert cli.main(["run", "--unit", FIXTURE, "--route", "auto"]) == 0

    out = capsys.readouterr().out
    assert "route auto echo kind=code tier=t0 mock" in out

    row = store.history()[0]
    assert (row.backend, row.tier, row.kind, row.ok) == ("mock", "t0", "code", True)

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--unit", FIXTURE, "--route", "auto", "--backend", "mock"])
    assert exc.value.code == 2


def test_run_sem_backend_e_sem_route_auto_sai_2(data_dir):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--unit", FIXTURE])
    assert exc.value.code == 2


def test_seed_workspace_copies_unit_files_except_unit_toml(tmp_path):
    unit = cli.load_unit(Path(__file__).parent / "fixtures" / "tiny_fix")
    ws = tmp_path / "ws"
    ws.mkdir()

    assert cli.seed_workspace(unit, ws) == ["target.py"]
    assert not (ws / "unit.toml").exists()
    assert "a - b" in (ws / "target.py").read_text()
