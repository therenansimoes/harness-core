"""O gate no caminho do `harness run`: KPI que regride força revert de verdade."""

import subprocess

import pytest

from harness import cli
from harness.ledger import store

UNIT = 'id = "u"\nkind = "code"\nprompt = "x"\nverify_cmd = "test -f mock_output.txt"\n'
MOCK_OUTPUT = "mock_output.txt"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def unit(tmp_path):
    d = tmp_path / "unit"
    d.mkdir()
    (d / "unit.toml").write_text(UNIT, encoding="utf-8")
    return str(d)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path, kpis: str):
    """Repo-alvo git, limpo, com um `kpis.toml` commitado."""
    repo = tmp_path / "alvo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "kpis.toml").write_text(kpis, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# "ls -1 | wc -l" com direction lower: o mock escreve mock_output.txt no
# workspace, então o depois tem mais arquivo que o antes = regressão.
REGRIDE = '[kpi.arquivos]\ncmd = "ls -1 | wc -l"\ndirection = "lower"\n'
ESTAVEL = '[kpi.fixo]\ncmd = "echo 7"\n'


def test_regressao_de_kpi_forca_revert(data_dir, unit, tmp_path):
    repo = _repo(tmp_path, REGRIDE)
    rc = cli.main(["run", "--unit", unit, "--backend", "mock", "--repo", str(repo)])

    assert rc == 1
    row = store.history()[0]
    assert row.ok is False
    assert row.exit_reason == "kpi_regression:arquivos"
    # revert de verdade: o que o backend escreveu não sobrevive ao gate.
    assert not (repo / MOCK_OUTPUT).exists()
    assert (repo / "kpis.toml").is_file()


def test_revert_aparece_no_stdout(data_dir, unit, tmp_path, capsys):
    cli.main(["run", "--unit", unit, "--backend", "mock", "--repo", str(_repo(tmp_path, REGRIDE))])
    out = capsys.readouterr().out
    assert "revert" in out and "kpi_regression:arquivos" in out


def test_sem_regressao_o_gate_aceita_e_nao_reverte(data_dir, unit, tmp_path):
    repo = _repo(tmp_path, ESTAVEL)
    rc = cli.main(["run", "--unit", unit, "--backend", "mock", "--repo", str(repo)])

    assert rc == 0
    assert store.history()[0].exit_reason == "done"
    assert (repo / MOCK_OUTPUT).is_file()


def test_verify_falho_e_retry_antes_de_olhar_kpi(data_dir, tmp_path):
    repo = _repo(tmp_path, ESTAVEL)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "unit.toml").write_text(
        'id = "bad"\nprompt = "x"\nverify_cmd = "exit 3"\n', encoding="utf-8"
    )
    assert cli.main(["run", "--unit", str(bad), "--backend", "mock", "--repo", str(repo)]) == 1
    assert store.history()[0].exit_reason == "verify_failed"


def test_verify_grava_log_fora_do_workspace(data_dir, unit, tmp_path):
    repo = _repo(tmp_path, ESTAVEL)
    cli.main(["run", "--unit", unit, "--backend", "mock", "--repo", str(repo)])
    assert not (repo / ".harness" / "verify.log").exists()


def test_repo_sujo_e_recusado_antes_de_executar(data_dir, unit, tmp_path):
    repo = _repo(tmp_path, ESTAVEL)
    (repo / "kpis.toml").write_text(ESTAVEL + "# mexido\n", encoding="utf-8")
    with pytest.raises(ValueError, match="não commitada"):
        cli.main(["run", "--unit", unit, "--backend", "mock", "--repo", str(repo)])
    assert not (repo / MOCK_OUTPUT).exists()


def test_repo_sem_git_e_recusado(data_dir, unit, tmp_path):
    plain = tmp_path / "sem-git"
    plain.mkdir()
    with pytest.raises(ValueError, match="não é um repo git"):
        cli.main(["run", "--unit", unit, "--backend", "mock", "--repo", str(plain)])
