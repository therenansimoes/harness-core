"""Projetos reais: init -> worktree do repo -> branch de entrega no accept."""

import subprocess
from pathlib import Path

import pytest

from harness import cli
from harness.graph.run_graph import run_unit
from harness.ledger import store
from harness.projects import get_project, init_project, load_projects
from harness.routing import CONFIG_DIR_ENV


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("HARNESS_DATA_DIR", str(d))
    return d


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """`config/` vazio: o registro de projetos do teste não encosta no do repo."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(cfg))
    return cfg


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _toy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "toyrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _unit(tmp_path: Path, toml: str, name: str = "unit") -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "unit.toml").write_text(toml, encoding="utf-8")
    return d


def _branches(repo: Path) -> set[str]:
    out = _git(repo, "branch", "--list", "harness/*", "--format=%(refname:short)")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def test_init_idempotente_e_recusa_nao_git(tmp_path, config_dir):
    repo = _toy_repo(tmp_path)
    q = tmp_path / "fila"
    init_project(repo, "toy", build_cmd="true", queue_dir=q)
    init_project(repo, "toy", build_cmd="true", queue_dir=q)  # re-init: sem duplicar
    projs = load_projects()
    assert set(projs) == {"toy"}
    assert projs["toy"].repo == repo.resolve()
    assert projs["toy"].build_cmd == "true"
    assert projs["toy"].queue_dir == q.resolve()
    assert q.is_dir()
    with pytest.raises(ValueError):
        init_project(tmp_path / "nao_git", "x", queue_dir=tmp_path / "qx")


def test_projeto_nao_registrado_falha_com_instrucao(tmp_path, config_dir):
    with pytest.raises(ValueError, match="harness init"):
        get_project("fantasma")


def test_cli_init_registra_e_status_uma_linha_por_projeto(
    tmp_path, config_dir, data_dir, capsys
):
    repo = _toy_repo(tmp_path)
    q = tmp_path / "fila"
    assert cli.main(["init", str(repo), "--name", "toy", "--queue-dir", str(q)]) == 0
    capsys.readouterr()
    (q / "pendente").mkdir()
    (q / "pendente" / "unit.toml").write_text('id = "p"\n', encoding="utf-8")

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("toy: fila=1 done=0 stuck=0")


def test_cli_init_recusa_diretorio_sem_git(tmp_path, config_dir, capsys):
    nao_git = tmp_path / "nao_git"
    nao_git.mkdir()
    assert cli.main(["init", str(nao_git), "--name", "x"]) == 1
    assert "não é repositório git" in capsys.readouterr().err


def test_verify_default_do_projeto(tmp_path, config_dir):
    repo = _toy_repo(tmp_path)
    init_project(repo, "toy", verify_default="test -f app.txt",
                 queue_dir=tmp_path / "fila")
    unit = _unit(tmp_path, 'id = "u4"\nproject = "toy"\nprompt = "x"\n')
    spec = cli.load_unit(unit)
    assert spec.verify_cmd == "test -f app.txt"
    assert spec.project == "toy"
    assert get_project("toy").verify_default == "test -f app.txt"


def test_unidade_sem_projeto_ainda_exige_verify_cmd(tmp_path, config_dir):
    unit = _unit(tmp_path, 'id = "u0"\nprompt = "x"\n')
    with pytest.raises(ValueError, match="verify_cmd"):
        cli.load_unit(unit)


def test_accept_entrega_branch_e_repo_fica_limpo(tmp_path, config_dir, data_dir):
    repo = _toy_repo(tmp_path)
    init_project(repo, "toy", queue_dir=tmp_path / "fila")
    unit = _unit(tmp_path, (
        'id = "u1"\nkind = "code"\nproject = "toy"\n'
        'prompt = "escreve a saída do mock"\n'
        'verify_cmd = "test -f mock_output.txt && test -f app.txt"\n'
    ))
    final = run_unit(unit, "mock", None, data_dir, thread_id="p-accept")
    assert final["decision"].action == "accept"

    assert _branches(repo) == {"harness/u1"}
    files = _git(repo, "show", "--name-only", "--format=", "harness/u1")
    assert "mock_output.txt" in files
    msg = _git(repo, "show", "-s", "--format=%B", "harness/u1")
    assert "run_id=" in msg and "cost_usd=" in msg
    # working tree principal intocada e sem worktree sobrando
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "worktree", "list").count("\n") == 1
    ws = data_dir / "ws"
    assert not ws.exists() or not any(ws.iterdir())
    rows = store.history()
    assert rows[0].project == "toy" and rows[0].ok


def test_escalate_remove_worktree_e_branch(tmp_path, config_dir, data_dir):
    repo = _toy_repo(tmp_path)
    init_project(repo, "toy", queue_dir=tmp_path / "fila")
    unit = _unit(tmp_path, (
        'id = "u2"\nkind = "code"\nproject = "toy"\n'
        'prompt = "x"\nverify_cmd = "false"\n'
    ))
    final = run_unit(unit, "mock", None, data_dir, thread_id="p-escalate",
                     max_attempts=1)
    assert final["decision"].action != "accept"
    assert _branches(repo) == set()
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "worktree", "list").count("\n") == 1


def test_build_cmd_roda_antes_do_verify(tmp_path, config_dir, data_dir):
    repo = _toy_repo(tmp_path)
    init_project(repo, "toy", build_cmd="touch built.txt", queue_dir=tmp_path / "fila")
    unit = _unit(tmp_path, (
        'id = "u3"\nkind = "code"\nproject = "toy"\n'
        'prompt = "x"\nverify_cmd = "test -f built.txt"\n'
    ))
    final = run_unit(unit, "mock", None, data_dir, thread_id="p-build")
    assert final["decision"].action == "accept"
    assert "harness/u3" in _branches(repo)


def test_sem_projeto_o_workspace_default_fica_intacto(tmp_path, config_dir, data_dir):
    """O worktree é opt-in: unidade sem `project` não encosta em git nenhum."""
    unit = _unit(tmp_path, (
        'id = "u5"\nkind = "code"\nprompt = "escreve a saída do mock"\n'
        'verify_cmd = "test -f mock_output.txt"\n'
    ))
    final = run_unit(unit, "mock", None, data_dir, thread_id="p-plain")
    assert final["decision"].action == "accept"
    ws = Path(final["workspace"])
    assert ws.is_dir() and (ws / "mock_output.txt").is_file()
    assert not (ws / ".git").exists()


def test_cli_run_de_unidade_com_projeto_entrega_branch(
    tmp_path, config_dir, data_dir, capsys
):
    """`harness run --unit` de projeto passa pelo grafo: sem isto o run no dedo
    rodava em tmpdir e a branch de entrega nunca existia."""
    repo = _toy_repo(tmp_path)
    init_project(repo, "toy", queue_dir=tmp_path / "fila")
    unit = _unit(tmp_path, (
        'id = "u6"\nkind = "code"\nproject = "toy"\n'
        'prompt = "escreve a saída do mock"\n'
        'verify_cmd = "test -f mock_output.txt && test -f app.txt"\n'
    ))
    assert cli.main(["run", "--unit", str(unit), "--backend", "mock"]) == 0

    out = capsys.readouterr().out.strip()
    assert " u6 mock accept " in out and "ledger#" in out
    assert _branches(repo) == {"harness/u6"}
    # worktree podado e working tree principal intocada
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "worktree", "list").count("\n") == 1
    assert store.history()[0].project == "toy"


def test_cli_run_sem_projeto_nao_passa_pelo_grafo(
    tmp_path, config_dir, data_dir, capsys, monkeypatch
):
    """Unidade sem `project`: fluxo inline como sempre foi. O grafo explodiria."""
    from harness.graph import run_graph

    def _boom(*a, **k):
        raise AssertionError("run_unit não deveria ser chamado sem project")

    monkeypatch.setattr(run_graph, "run_unit", _boom)
    unit = _unit(tmp_path, (
        'id = "u7"\nkind = "code"\nprompt = "escreve a saída do mock"\n'
        'verify_cmd = "test -f mock_output.txt"\n'
    ))
    assert cli.main(["run", "--unit", str(unit), "--backend", "mock"]) == 0
    assert " u7 mock accept " in capsys.readouterr().out
